# ROB-101 Preopen Execution Review Panel & Basket Preview UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear:** https://linear.app/mgh3326/issue/ROB-101/preopen-execution-review-panel-and-basket-preview-ui
**Depends on:** ROB-100 / PR #670 — `app.schemas.execution_contracts` (already merged on `main`).

**Goal:** Expand the `/preopen` page from a briefing/readiness surface into a read-only execution review surface with stage statuses and an optional order-basket preview, reusing the ROB-100 shared contract.

**Architecture:** Additive. We add a single optional field `execution_review: ExecutionReviewSummary | None` to `PreopenLatestResponse`. The dashboard service builds it deterministically from already-loaded data (research run + candidates + news + reconciliations) without importing any broker / order / watch / intent / credentials modules. Conservative defaults are baked into the schema (`execution_allowed=False`, `approval_required=True`, `is_ready=False`, `advisory_only=True`). The frontend renders a new `ExecutionReviewPanel` component with stage chips, a guardrail banner, and basket-preview cards.

**Tech Stack:** Python 3.13, Pydantic v2, FastAPI, SQLAlchemy async (read-only), pytest. React 18 + TypeScript + Vite + Vitest + React Testing Library. CSS Modules.

**Out of scope (per Linear):**
- No KIS mock/live order submit, no watch alert / order intent / scheduler side effects.
- No mock reconciliation worker.
- No websocket runtime split.

---

## File Structure

### Backend
- **Modify** `app/schemas/preopen.py` — add `ExecutionReviewStage`, `ExecutionReviewSummary`; add `execution_review` field to `PreopenLatestResponse`.
- **Modify** `app/services/preopen_dashboard_service.py` — add `_build_execution_review(...)`, wire it into `_FAIL_OPEN` and `get_latest_preopen_dashboard`.
- **Create** `tests/test_preopen_execution_review.py` — unit tests for the new builder and the new field on `PreopenLatestResponse`.
- **Modify** `tests/test_router_preopen.py` — assert the new field is present and read-only-shaped in the router response.

### Frontend
- **Modify** `frontend/trading-decision/src/api/types.ts` — mirror new types (`ExecutionReviewStage`, `ExecutionReviewSummary`, plus the four ROB-100 contract types).
- **Modify** `frontend/trading-decision/src/test/fixtures/preopen.ts` — add `makePreopenExecutionReview*` factories; thread the field through `makePreopenResponse` / `makePreopenFailOpen`.
- **Create** `frontend/trading-decision/src/components/ExecutionReviewPanel.tsx` + `.module.css` — render stage list, guardrail banner, optional basket preview cards.
- **Modify** `frontend/trading-decision/src/pages/PreopenPage.tsx` — wire `<ExecutionReviewPanel ... />` into the page, both has-run and no-run branches.
- **Create** `frontend/trading-decision/src/__tests__/ExecutionReviewPanel.test.tsx` — component-level tests.
- **Modify** `frontend/trading-decision/src/__tests__/PreopenPage.test.tsx` — page-level integration assertions for execution review panel.

### Coding rules to enforce in this branch
- The schema/service code MUST NOT import `app.kis*`, `app.services.kis*`, broker, watch, order intent, or credential modules.
- Frontend forbidden-import test (`forbidden_mutation_imports.test.ts`) must keep passing — no new mutation-style symbols.
- Reuse `ExecutionReadiness`, `ExecutionGuard`, `OrderPreviewLine`, `OrderBasketPreview` directly from `app.schemas.execution_contracts`. Do not re-define their fields.

---

## Task 1: Schema additions in `app/schemas/preopen.py`

**Files:**
- Modify: `app/schemas/preopen.py`
- Test: `tests/test_preopen_execution_review.py` (new file)

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_preopen_execution_review.py`:

```python
"""Schema tests for ROB-101 execution review additions."""

from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.mark.unit
def test_execution_review_summary_defaults_are_advisory_and_blocked():
    from app.schemas.execution_contracts import ExecutionGuard, ExecutionReadiness
    from app.schemas.preopen import ExecutionReviewStage, ExecutionReviewSummary

    readiness = ExecutionReadiness(
        account_mode="db_simulated",
        execution_source="preopen",
        is_ready=False,
        guard=ExecutionGuard(
            execution_allowed=False,
            approval_required=True,
            blocking_reasons=["mvp_read_only"],
        ),
    )
    summary = ExecutionReviewSummary(
        readiness=readiness,
        stages=[
            ExecutionReviewStage(
                stage_id="data_news",
                label="Data / news readiness",
                status="ready",
                summary="news ready",
            )
        ],
    )

    assert summary.advisory_only is True
    assert summary.execution_allowed is False
    assert summary.basket_preview is None
    assert summary.readiness.guard.execution_allowed is False
    assert summary.readiness.guard.approval_required is True
    assert summary.contract_version == "v1"


@pytest.mark.unit
def test_execution_review_stage_status_literal_is_strict():
    from app.schemas.preopen import ExecutionReviewStage

    with pytest.raises(Exception):
        ExecutionReviewStage(
            stage_id="data_news",
            label="x",
            status="bogus",  # type: ignore[arg-type]
            summary="x",
        )


@pytest.mark.unit
def test_execution_review_stage_id_literal_is_strict():
    from app.schemas.preopen import ExecutionReviewStage

    with pytest.raises(Exception):
        ExecutionReviewStage(
            stage_id="not_a_stage",  # type: ignore[arg-type]
            label="x",
            status="ready",
            summary="x",
        )


@pytest.mark.unit
def test_preopen_response_accepts_execution_review_field():
    from app.schemas.execution_contracts import ExecutionGuard, ExecutionReadiness
    from app.schemas.preopen import (
        ExecutionReviewStage,
        ExecutionReviewSummary,
        PreopenLatestResponse,
    )

    review = ExecutionReviewSummary(
        readiness=ExecutionReadiness(
            account_mode="db_simulated",
            execution_source="preopen",
            is_ready=False,
            guard=ExecutionGuard(
                execution_allowed=False,
                approval_required=True,
                blocking_reasons=["mvp_read_only"],
            ),
        ),
        stages=[
            ExecutionReviewStage(
                stage_id="approval_required",
                label="Approval required",
                status="pending",
                summary="Mock execution requires explicit operator approval.",
            )
        ],
    )

    response = PreopenLatestResponse(
        has_run=False,
        run_uuid=None,
        market_scope=None,
        stage=None,
        status=None,
        strategy_name=None,
        source_profile=None,
        generated_at=None,
        created_at=None,
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=0,
        reconciliation_count=0,
        candidates=[],
        reconciliations=[],
        linked_sessions=[],
        execution_review=review,
    )

    assert response.execution_review is not None
    assert response.execution_review.advisory_only is True


@pytest.mark.unit
def test_preopen_response_execution_review_is_optional_for_backward_compat():
    from app.schemas.preopen import PreopenLatestResponse

    response = PreopenLatestResponse(
        has_run=False,
        run_uuid=None,
        market_scope=None,
        stage=None,
        status=None,
        strategy_name=None,
        source_profile=None,
        generated_at=None,
        created_at=None,
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=0,
        reconciliation_count=0,
        candidates=[],
        reconciliations=[],
        linked_sessions=[],
    )
    assert response.execution_review is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_preopen_execution_review.py -v`
Expected: FAIL with `ImportError` / `AttributeError` for `ExecutionReviewStage`, `ExecutionReviewSummary`, or with a `ValidationError` because `execution_review` is not a known field on `PreopenLatestResponse`.

- [ ] **Step 3: Add the schema definitions**

Edit `app/schemas/preopen.py`:

(a) Add to the imports at the top (next to `from app.schemas.preopen_news_brief import KRPreopenNewsBrief`):

```python
from app.schemas.execution_contracts import (
    ExecutionGuard,
    ExecutionReadiness,
    OrderBasketPreview,
)
```

(b) Add the new literals and models above `class PreopenLatestResponse(BaseModel):`:

```python
ExecutionReviewStageId = Literal[
    "data_news",
    "candidate_review",
    "cash_holdings_quotes",
    "basket_preview",
    "approval_required",
    "post_order_reconcile",
]
ExecutionReviewStageStatus = Literal[
    "ready",
    "degraded",
    "unavailable",
    "skipped",
    "pending",
]


class ExecutionReviewStage(BaseModel):
    stage_id: ExecutionReviewStageId
    label: str
    status: ExecutionReviewStageStatus
    summary: str
    warnings: list[str] = []
    details: dict[str, Any] = {}


class ExecutionReviewSummary(BaseModel):
    contract_version: Literal["v1"] = "v1"
    advisory_only: Literal[True] = True
    execution_allowed: Literal[False] = False
    readiness: ExecutionReadiness
    stages: list[ExecutionReviewStage] = []
    basket_preview: OrderBasketPreview | None = None
    blocking_reasons: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
```

(c) Add the optional field to `PreopenLatestResponse` (right after `paper_approval_bridge: PreopenPaperApprovalBridge | None = None`):

```python
    execution_review: ExecutionReviewSummary | None = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_preopen_execution_review.py -v`
Expected: PASS for all five tests.

- [ ] **Step 5: Run the existing preopen schema/router tests to confirm no regressions**

Run: `uv run pytest tests/test_router_preopen.py tests/test_preopen_dashboard_service.py -v`
Expected: PASS unchanged (the new field defaults to `None` and is additive).

- [ ] **Step 6: Commit**

```bash
git add app/schemas/preopen.py tests/test_preopen_execution_review.py
git commit -m "feat(ROB-101): add ExecutionReviewSummary schema to preopen response"
```

---

## Task 2: Service builder `_build_execution_review` (no-run / fail-open path)

**Files:**
- Modify: `app/services/preopen_dashboard_service.py`
- Test: `tests/test_preopen_execution_review.py`

- [ ] **Step 1: Write the failing test (no-run path)**

Append to `tests/test_preopen_execution_review.py`:

```python
@pytest.mark.unit
def test_build_execution_review_no_run_is_unavailable_and_blocked():
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=False,
        market_scope="kr",
        stage="preopen",
        candidates=[],
        reconciliations=[],
        news=None,
        briefing_artifact=None,
    )

    assert review.advisory_only is True
    assert review.execution_allowed is False
    assert review.readiness.is_ready is False
    assert "mvp_read_only" in review.readiness.guard.blocking_reasons
    assert "no_open_preopen_run" in review.readiness.guard.blocking_reasons
    assert review.basket_preview is None

    stage_ids = {s.stage_id for s in review.stages}
    assert stage_ids == {
        "data_news",
        "candidate_review",
        "cash_holdings_quotes",
        "basket_preview",
        "approval_required",
        "post_order_reconcile",
    }
    candidate_stage = next(s for s in review.stages if s.stage_id == "candidate_review")
    assert candidate_stage.status == "unavailable"
    cash_stage = next(s for s in review.stages if s.stage_id == "cash_holdings_quotes")
    assert cash_stage.status == "unavailable"
    assert "not_in_current_preopen_contract" in cash_stage.warnings
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_preopen_execution_review.py::test_build_execution_review_no_run_is_unavailable_and_blocked -v`
Expected: FAIL with `ImportError` because `_build_execution_review` does not exist.

- [ ] **Step 3: Add the builder to the service**

Edit `app/services/preopen_dashboard_service.py`:

(a) Add to the imports (next to other `from app.schemas.preopen import (...)` block):

```python
from app.schemas.execution_contracts import (
    ExecutionGuard,
    ExecutionReadiness,
    OrderBasketPreview,
    OrderPreviewLine,
)
from app.schemas.preopen import (
    ExecutionReviewStage,
    ExecutionReviewSummary,
)
```

(Merge with the existing `from app.schemas.preopen import (...)` rather than duplicating it; add only the two new names.)

(b) Add the builder near the bottom of the file, before `async def get_latest_preopen_dashboard`:

```python
def _build_execution_review(
    *,
    has_run: bool,
    market_scope: str | None,
    stage: str | None,
    candidates: list[CandidateSummary],
    reconciliations: list[ReconciliationSummary],
    news: NewsReadinessSummary | None,
    briefing_artifact: PreopenBriefingArtifact | None,
) -> ExecutionReviewSummary:
    """Deterministic, read-only execution review built from already-loaded data.

    Always returns an ``advisory_only=True`` summary with conservative
    ``ExecutionGuard`` defaults. No broker / order / watch / credential code is
    consulted: ``cash``, ``holdings``, ``quotes``, ``broker_order``, and
    ``watch`` are reported as ``unavailable`` with the
    ``not_in_current_preopen_contract`` warning so the UI can render them as
    degraded rather than silently dropping them.
    """

    blocking_reasons: list[str] = ["mvp_read_only"]
    warnings: list[str] = []

    if not has_run:
        blocking_reasons.append("no_open_preopen_run")

    news_status = news.status if news is not None else "unavailable"
    if news is None or not news.is_ready:
        blocking_reasons.append(f"news_{news_status}")

    buy_candidates = [c for c in candidates if c.side == "buy"]

    stages: list[ExecutionReviewStage] = []

    # 1) data / news
    if news is None:
        data_status: str = "unavailable"
        data_summary = "News readiness is unavailable."
        data_warnings = ["news_readiness_unavailable"]
    elif news.is_ready:
        data_status = "ready"
        data_summary = "News readiness is fresh."
        data_warnings = []
    else:
        data_status = "degraded"
        data_summary = f"News readiness is {news.status}."
        data_warnings = list(news.warnings)
    stages.append(
        ExecutionReviewStage(
            stage_id="data_news",
            label="Data / news readiness",
            status=data_status,  # type: ignore[arg-type]
            summary=data_summary,
            warnings=data_warnings,
            details={"news_status": news_status},
        )
    )

    # 2) candidate review
    if not has_run:
        candidate_status = "unavailable"
        candidate_summary = "No open preopen research run."
    elif not candidates:
        candidate_status = "degraded"
        candidate_summary = "Open run has no candidates."
    else:
        candidate_status = "ready"
        candidate_summary = (
            f"{len(candidates)} candidates ({len(buy_candidates)} buy)."
        )
    stages.append(
        ExecutionReviewStage(
            stage_id="candidate_review",
            label="Candidate review",
            status=candidate_status,  # type: ignore[arg-type]
            summary=candidate_summary,
            details={
                "candidate_count": len(candidates),
                "buy_candidate_count": len(buy_candidates),
            },
        )
    )

    # 3) cash / holdings / quotes (always unavailable in MVP read-only)
    stages.append(
        ExecutionReviewStage(
            stage_id="cash_holdings_quotes",
            label="Cash / holdings / quotes check",
            status="unavailable",
            summary="Live cash, holdings, and quotes lookups are not wired in this MVP.",
            warnings=["not_in_current_preopen_contract"],
            details={},
        )
    )

    # 4) basket preview
    basket_preview: OrderBasketPreview | None = None
    if buy_candidates:
        readiness_for_basket = ExecutionReadiness(
            account_mode="db_simulated",
            execution_source="preopen",
            is_ready=False,
            guard=ExecutionGuard(
                execution_allowed=False,
                approval_required=True,
                blocking_reasons=["mvp_read_only"],
            ),
        )
        basket_preview = OrderBasketPreview(
            account_mode="db_simulated",
            execution_source="preopen",
            readiness=readiness_for_basket,
            lines=[
                OrderPreviewLine(
                    symbol=c.symbol,
                    market=market_scope or "kr",
                    side="buy",
                    account_mode="db_simulated",
                    execution_source="preopen",
                    quantity=c.proposed_qty,
                    limit_price=c.proposed_price,
                    currency=c.currency,
                    guard=ExecutionGuard(
                        execution_allowed=False,
                        approval_required=True,
                        blocking_reasons=["mvp_read_only"],
                    ),
                    rationale=[c.rationale] if c.rationale else [],
                )
                for c in buy_candidates
            ],
            basket_warnings=["mvp_read_only"],
        )
        basket_status = "ready"
        basket_summary = (
            f"{len(buy_candidates)} buy candidates rendered as a basket preview."
        )
    elif has_run:
        basket_status = "degraded"
        basket_summary = "No buy candidates available for basket preview."
    else:
        basket_status = "unavailable"
        basket_summary = "No open run to derive a basket from."
    stages.append(
        ExecutionReviewStage(
            stage_id="basket_preview",
            label="Basket preview",
            status=basket_status,  # type: ignore[arg-type]
            summary=basket_summary,
            details={"line_count": len(basket_preview.lines) if basket_preview else 0},
        )
    )

    # 5) approval required
    stages.append(
        ExecutionReviewStage(
            stage_id="approval_required",
            label="Approval required",
            status="pending",
            summary=(
                "Mock execution requires later explicit operator approval. "
                "This page does not submit orders."
            ),
            details={"advisory_only": True, "execution_allowed": False},
        )
    )

    # 6) post-order reconcile
    pending_recs = len(reconciliations)
    if not has_run:
        recon_status = "unavailable"
        recon_summary = "No open run to drive reconciliation."
    elif pending_recs == 0:
        recon_status = "skipped"
        recon_summary = "No pending reconciliations on the latest run."
    else:
        recon_status = "pending"
        recon_summary = f"{pending_recs} pending reconciliations to review."
    stages.append(
        ExecutionReviewStage(
            stage_id="post_order_reconcile",
            label="Post-order reconciliation",
            status=recon_status,  # type: ignore[arg-type]
            summary=recon_summary,
            details={"pending_reconciliation_count": pending_recs},
        )
    )

    # Lift any per-stage warnings into the summary warnings (de-duplicated).
    for s in stages:
        for w in s.warnings:
            if w not in warnings:
                warnings.append(w)

    readiness = ExecutionReadiness(
        account_mode="db_simulated",
        execution_source="preopen",
        is_ready=False,
        guard=ExecutionGuard(
            execution_allowed=False,
            approval_required=True,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
        ),
        notes=[
            "Advisory read-only review; no broker submit on this page.",
        ],
    )

    notes: list[str] = [
        "advisory_only",
        "no_live_execution",
        "mock_execution_requires_explicit_approval",
    ]

    return ExecutionReviewSummary(
        readiness=readiness,
        stages=stages,
        basket_preview=basket_preview,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        notes=notes,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_preopen_execution_review.py::test_build_execution_review_no_run_is_unavailable_and_blocked -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/preopen_dashboard_service.py tests/test_preopen_execution_review.py
git commit -m "feat(ROB-101): build read-only execution review from preopen DTOs"
```

---

## Task 3: Builder coverage for the has-run path (candidates, reconciliations, basket)

**Files:**
- Test: `tests/test_preopen_execution_review.py`
- (No source change expected — Task 2's implementation already covers the has-run case; this task verifies it.)

- [ ] **Step 1: Write the failing has-run tests**

Append to `tests/test_preopen_execution_review.py`:

```python
def _candidate(side: str, symbol: str, qty: str, price: str) -> object:
    from app.schemas.preopen import CandidateSummary
    from uuid import uuid4

    return CandidateSummary(
        candidate_uuid=uuid4(),
        symbol=symbol,
        instrument_type="equity_kr",
        side=side,  # type: ignore[arg-type]
        candidate_kind="proposed",
        proposed_price=Decimal(price),
        proposed_qty=Decimal(qty),
        confidence=70,
        rationale=f"reason for {symbol}",
        currency="KRW",
        warnings=[],
    )


def _ready_news() -> object:
    from app.schemas.preopen import NewsReadinessSummary

    return NewsReadinessSummary(
        status="ready",
        is_ready=True,
        is_stale=False,
        latest_run_uuid="news-1",
        latest_status="success",
        latest_finished_at=None,
        latest_article_published_at=None,
        source_counts={},
        source_coverage=[],
        warnings=[],
        max_age_minutes=180,
    )


@pytest.mark.unit
def test_build_execution_review_with_buy_candidates_emits_basket_preview():
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=True,
        market_scope="kr",
        stage="preopen",
        candidates=[
            _candidate("buy", "005930", "10", "70000"),
            _candidate("buy", "035720", "5", "60000"),
            _candidate("sell", "000660", "1", "120000"),
        ],
        reconciliations=[],
        news=_ready_news(),
        briefing_artifact=None,
    )

    basket = review.basket_preview
    assert basket is not None
    assert basket.account_mode == "db_simulated"
    assert basket.execution_source == "preopen"
    assert [line.symbol for line in basket.lines] == ["005930", "035720"]
    for line in basket.lines:
        assert line.guard.execution_allowed is False
        assert line.guard.approval_required is True

    candidate_stage = next(s for s in review.stages if s.stage_id == "candidate_review")
    assert candidate_stage.status == "ready"
    basket_stage = next(s for s in review.stages if s.stage_id == "basket_preview")
    assert basket_stage.status == "ready"
    assert basket_stage.details["line_count"] == 2

    # ``mvp_read_only`` always blocks even when news is fresh and run is open.
    assert "mvp_read_only" in review.readiness.guard.blocking_reasons


@pytest.mark.unit
def test_build_execution_review_pending_reconciliations_marked_pending():
    from app.schemas.preopen import ReconciliationSummary
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=True,
        market_scope="kr",
        stage="preopen",
        candidates=[],
        reconciliations=[
            ReconciliationSummary(
                order_id="ORD-1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="near_fill",
                nxt_classification=None,
                nxt_actionable=None,
                gap_pct=Decimal("0.5"),
                summary="near fill",
                reasons=[],
                warnings=[],
            )
        ],
        news=_ready_news(),
        briefing_artifact=None,
    )

    recon_stage = next(s for s in review.stages if s.stage_id == "post_order_reconcile")
    assert recon_stage.status == "pending"
    assert recon_stage.details["pending_reconciliation_count"] == 1


@pytest.mark.unit
def test_build_execution_review_lines_match_basket_invariant_holds():
    """OrderBasketPreview's own validator must accept what we emit."""
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=True,
        market_scope="kr",
        stage="preopen",
        candidates=[_candidate("buy", "005930", "1", "1")],
        reconciliations=[],
        news=_ready_news(),
        briefing_artifact=None,
    )
    basket = review.basket_preview
    assert basket is not None
    for line in basket.lines:
        assert line.account_mode == basket.account_mode
        assert line.execution_source == basket.execution_source
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_preopen_execution_review.py -v`
Expected: PASS for all the new has-run tests (and previously added ones).

- [ ] **Step 3: Commit**

```bash
git add tests/test_preopen_execution_review.py
git commit -m "test(ROB-101): cover has-run path of execution review builder"
```

---

## Task 4: Wire `execution_review` into the dashboard response (no-run + has-run paths)

**Files:**
- Modify: `app/services/preopen_dashboard_service.py` (`_FAIL_OPEN` and `get_latest_preopen_dashboard`)
- Test: `tests/test_preopen_dashboard_service.py`, `tests/test_router_preopen.py`

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_preopen_execution_review.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_latest_dashboard_no_run_includes_execution_review(monkeypatch):
    from app.services import preopen_dashboard_service, research_run_service

    async def _no_run(*args, **kwargs):
        return None

    monkeypatch.setattr(
        research_run_service, "get_latest_research_run", _no_run
    )

    fake_db = MagicMock()
    response = await preopen_dashboard_service.get_latest_preopen_dashboard(
        fake_db, user_id=1, market_scope="kr", stage="preopen"
    )

    review = response.execution_review
    assert review is not None
    assert review.advisory_only is True
    assert review.execution_allowed is False
    assert "no_open_preopen_run" in review.readiness.guard.blocking_reasons
```

(Add `from unittest.mock import MagicMock` near the top of the file if not already imported.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_preopen_execution_review.py::test_get_latest_dashboard_no_run_includes_execution_review -v`
Expected: FAIL — `response.execution_review` is `None` until we wire it.

- [ ] **Step 3: Wire the field through the no-run branch in `get_latest_preopen_dashboard`**

Edit `app/services/preopen_dashboard_service.py`. In the `if run is None:` branch (where `qa_evaluator` and `paper_approval_bridge` are built), add:

```python
        execution_review = _build_execution_review(
            has_run=False,
            market_scope=market_scope,
            stage=stage,
            candidates=[],
            reconciliations=[],
            news=None,
            briefing_artifact=_FAIL_OPEN.briefing_artifact,
        )
        return _FAIL_OPEN.model_copy(
            update={
                "market_scope": market_scope,
                "stage": stage,
                "qa_evaluator": qa_evaluator,
                "paper_approval_bridge": paper_approval_bridge,
                "execution_review": execution_review,
            }
        )
```

- [ ] **Step 4: Wire the field through the has-run return**

In the same function, at the bottom where `PreopenLatestResponse(...)` is constructed, build and pass `execution_review`:

```python
    execution_review = _build_execution_review(
        has_run=True,
        market_scope=run.market_scope,
        stage=stage,
        candidates=candidates,
        reconciliations=reconciliations,
        news=news_summary,
        briefing_artifact=briefing_artifact,
    )

    return PreopenLatestResponse(
        # ... all existing kwargs unchanged ...
        paper_approval_bridge=paper_approval_bridge,
        execution_review=execution_review,
    )
```

(Keep every existing kwarg; just append `execution_review=execution_review`.)

- [ ] **Step 5: Run the wiring test plus existing dashboard/router tests**

Run: `uv run pytest tests/test_preopen_execution_review.py tests/test_preopen_dashboard_service.py tests/test_router_preopen.py -v`
Expected: PASS (no regressions; new wiring test passes).

- [ ] **Step 6: Add a router-level assertion that the field is exposed read-only**

Append to `tests/test_router_preopen.py`:

```python
@pytest.mark.unit
def test_get_latest_preopen_returns_execution_review_field(monkeypatch):
    """The response payload must include execution_review with advisory-only defaults."""
    from app.services import preopen_dashboard_service

    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=_fail_open_response_with_review()),
    )

    app = _app()
    client = TestClient(app)
    res = client.get(ENDPOINT, params={"market_scope": "kr"})
    assert res.status_code == 200
    body = res.json()
    assert "execution_review" in body
    review = body["execution_review"]
    assert review is not None
    assert review["advisory_only"] is True
    assert review["execution_allowed"] is False
    assert review["readiness"]["guard"]["execution_allowed"] is False
```

Add the helper at the top of the same file (next to `_fail_open_response`):

```python
def _fail_open_response_with_review() -> "PreopenLatestResponse":  # noqa: F821
    from app.schemas.execution_contracts import ExecutionGuard, ExecutionReadiness
    from app.schemas.preopen import (
        ExecutionReviewStage,
        ExecutionReviewSummary,
        PreopenLatestResponse,
    )

    base = _fail_open_response()
    review = ExecutionReviewSummary(
        readiness=ExecutionReadiness(
            account_mode="db_simulated",
            execution_source="preopen",
            is_ready=False,
            guard=ExecutionGuard(
                execution_allowed=False,
                approval_required=True,
                blocking_reasons=["mvp_read_only", "no_open_preopen_run"],
            ),
        ),
        stages=[
            ExecutionReviewStage(
                stage_id="approval_required",
                label="Approval required",
                status="pending",
                summary="Read-only.",
            )
        ],
        blocking_reasons=["mvp_read_only", "no_open_preopen_run"],
    )
    return base.model_copy(update={"execution_review": review})
```

- [ ] **Step 7: Run the router test**

Run: `uv run pytest tests/test_router_preopen.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/preopen_dashboard_service.py tests/test_preopen_execution_review.py tests/test_router_preopen.py
git commit -m "feat(ROB-101): expose execution_review on preopen latest endpoint"
```

---

## Task 5: Forbidden-imports verification (backend safety)

**Files:**
- Test: `tests/test_preopen_execution_review.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preopen_execution_review.py`:

```python
@pytest.mark.unit
def test_dashboard_service_does_not_import_broker_or_order_modules():
    """ROB-101 must keep the preopen aggregation read-only."""
    import ast
    from pathlib import Path

    src = Path("app/services/preopen_dashboard_service.py").read_text()
    tree = ast.parse(src)

    forbidden_prefixes = (
        "app.kis",
        "app.services.kis",
        "app.services.kis_trading_service",
        "app.services.paper_order_handler",
        "app.services.watch_alerts",
        "app.services.order_intent",
        "app.services.alpaca",
    )

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module == p or module.startswith(p + ".") for p in forbidden_prefixes):
                found.append(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == p or alias.name.startswith(p + ".")
                    for p in forbidden_prefixes
                ):
                    found.append(alias.name)

    assert found == [], f"forbidden imports leaked into preopen service: {found}"
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_preopen_execution_review.py::test_dashboard_service_does_not_import_broker_or_order_modules -v`
Expected: PASS (the implementation in Task 2 only imports schema modules).

- [ ] **Step 3: Commit**

```bash
git add tests/test_preopen_execution_review.py
git commit -m "test(ROB-101): pin read-only import policy for preopen dashboard service"
```

---

## Task 6: Frontend type mirror in `api/types.ts`

**Files:**
- Modify: `frontend/trading-decision/src/api/types.ts`

- [ ] **Step 1: Append new types after `PreopenPaperApprovalBridge`**

Edit `frontend/trading-decision/src/api/types.ts`. Add immediately after the `export interface PreopenPaperApprovalBridge { ... }` block:

```typescript
// ROB-100 contract types mirrored on the client.
export type ExecutionAccountMode =
  | "kis_live"
  | "kis_mock"
  | "alpaca_paper"
  | "db_simulated";
export type ExecutionSource =
  | "preopen"
  | "watch"
  | "manual"
  | "websocket"
  | "reconciler";
export type OrderLifecycleState =
  | "planned"
  | "previewed"
  | "submitted"
  | "accepted"
  | "pending"
  | "fill"
  | "reconciled"
  | "stale"
  | "failed"
  | "anomaly";

export interface ExecutionGuard {
  execution_allowed: boolean;
  approval_required: boolean;
  blocking_reasons: string[];
  warnings: string[];
}

export interface ExecutionReadiness {
  contract_version: "v1";
  account_mode: ExecutionAccountMode;
  execution_source: ExecutionSource;
  is_ready: boolean;
  guard: ExecutionGuard;
  checked_at: IsoDateTime | null;
  notes: string[];
}

export interface OrderPreviewLine {
  contract_version: "v1";
  symbol: string;
  market: string;
  side: "buy" | "sell";
  account_mode: ExecutionAccountMode;
  execution_source: ExecutionSource;
  lifecycle_state: OrderLifecycleState;
  quantity: DecimalString | null;
  limit_price: DecimalString | null;
  notional: DecimalString | null;
  currency: string | null;
  guard: ExecutionGuard;
  rationale: string[];
  correlation_id: string | null;
}

export interface OrderBasketPreview {
  contract_version: "v1";
  account_mode: ExecutionAccountMode;
  execution_source: ExecutionSource;
  readiness: ExecutionReadiness;
  lines: OrderPreviewLine[];
  basket_warnings: string[];
}

// ROB-101 execution review types.
export type ExecutionReviewStageId =
  | "data_news"
  | "candidate_review"
  | "cash_holdings_quotes"
  | "basket_preview"
  | "approval_required"
  | "post_order_reconcile";

export type ExecutionReviewStageStatus =
  | "ready"
  | "degraded"
  | "unavailable"
  | "skipped"
  | "pending";

export interface ExecutionReviewStage {
  stage_id: ExecutionReviewStageId;
  label: string;
  status: ExecutionReviewStageStatus;
  summary: string;
  warnings: string[];
  details: Record<string, unknown>;
}

export interface ExecutionReviewSummary {
  contract_version: "v1";
  advisory_only: true;
  execution_allowed: false;
  readiness: ExecutionReadiness;
  stages: ExecutionReviewStage[];
  basket_preview: OrderBasketPreview | null;
  blocking_reasons: string[];
  warnings: string[];
  notes: string[];
}
```

- [ ] **Step 2: Add `execution_review` to `PreopenLatestResponse`**

In the same file, inside the existing `export interface PreopenLatestResponse { ... }`, add after the `paper_approval_bridge: PreopenPaperApprovalBridge | null;` line:

```typescript
  execution_review: ExecutionReviewSummary | null;
```

- [ ] **Step 3: Run the typecheck/tests so far**

Run from `frontend/trading-decision/`:
```bash
npx tsc --noEmit
```
Expected: PASS (the new types are additive).

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/api/types.ts
git commit -m "feat(ROB-101): mirror execution review + ROB-100 contract types on client"
```

---

## Task 7: Frontend fixtures for execution review

**Files:**
- Modify: `frontend/trading-decision/src/test/fixtures/preopen.ts`

- [ ] **Step 1: Add factories at the end of the file**

Edit `frontend/trading-decision/src/test/fixtures/preopen.ts`.

(a) Extend the imports at the top to include the new types:

```typescript
import type {
  ExecutionReviewStage,
  ExecutionReviewSummary,
  OrderBasketPreview,
  PreopenBriefingArtifact,
  PreopenCandidateSummary,
  PreopenLatestResponse,
  PreopenLinkedSession,
  PreopenMarketNewsBriefing,
  PreopenMarketNewsItem,
  PreopenNewsArticlePreview,
  PreopenNewsReadinessSummary,
  PreopenPaperApprovalBridge,
  PreopenQaEvaluatorSummary,
  PreopenReconciliationSummary,
} from "../../api/types";
```

(b) Append new factories before the closing of the file:

```typescript
export function makePreopenExecutionReviewBasket(
  overrides: Partial<OrderBasketPreview> = {},
): OrderBasketPreview {
  return {
    contract_version: "v1",
    account_mode: "db_simulated",
    execution_source: "preopen",
    readiness: {
      contract_version: "v1",
      account_mode: "db_simulated",
      execution_source: "preopen",
      is_ready: false,
      guard: {
        execution_allowed: false,
        approval_required: true,
        blocking_reasons: ["mvp_read_only"],
        warnings: [],
      },
      checked_at: null,
      notes: ["Advisory read-only review; no broker submit on this page."],
    },
    lines: [
      {
        contract_version: "v1",
        symbol: "005930",
        market: "kr",
        side: "buy",
        account_mode: "db_simulated",
        execution_source: "preopen",
        lifecycle_state: "previewed",
        quantity: "10",
        limit_price: "70000",
        notional: null,
        currency: "KRW",
        guard: {
          execution_allowed: false,
          approval_required: true,
          blocking_reasons: ["mvp_read_only"],
          warnings: [],
        },
        rationale: ["Strong momentum play"],
        correlation_id: null,
      },
    ],
    basket_warnings: ["mvp_read_only"],
    ...overrides,
  };
}

const DEFAULT_REVIEW_STAGES: ExecutionReviewStage[] = [
  {
    stage_id: "data_news",
    label: "Data / news readiness",
    status: "ready",
    summary: "News readiness is fresh.",
    warnings: [],
    details: { news_status: "ready" },
  },
  {
    stage_id: "candidate_review",
    label: "Candidate review",
    status: "ready",
    summary: "1 candidates (1 buy).",
    warnings: [],
    details: { candidate_count: 1, buy_candidate_count: 1 },
  },
  {
    stage_id: "cash_holdings_quotes",
    label: "Cash / holdings / quotes check",
    status: "unavailable",
    summary: "Live cash, holdings, and quotes lookups are not wired in this MVP.",
    warnings: ["not_in_current_preopen_contract"],
    details: {},
  },
  {
    stage_id: "basket_preview",
    label: "Basket preview",
    status: "ready",
    summary: "1 buy candidates rendered as a basket preview.",
    warnings: [],
    details: { line_count: 1 },
  },
  {
    stage_id: "approval_required",
    label: "Approval required",
    status: "pending",
    summary:
      "Mock execution requires later explicit operator approval. This page does not submit orders.",
    warnings: [],
    details: { advisory_only: true, execution_allowed: false },
  },
  {
    stage_id: "post_order_reconcile",
    label: "Post-order reconciliation",
    status: "skipped",
    summary: "No pending reconciliations on the latest run.",
    warnings: [],
    details: { pending_reconciliation_count: 0 },
  },
];

export function makePreopenExecutionReview(
  overrides: Partial<ExecutionReviewSummary> = {},
): ExecutionReviewSummary {
  return {
    contract_version: "v1",
    advisory_only: true,
    execution_allowed: false,
    readiness: {
      contract_version: "v1",
      account_mode: "db_simulated",
      execution_source: "preopen",
      is_ready: false,
      guard: {
        execution_allowed: false,
        approval_required: true,
        blocking_reasons: ["mvp_read_only"],
        warnings: ["not_in_current_preopen_contract"],
      },
      checked_at: null,
      notes: ["Advisory read-only review; no broker submit on this page."],
    },
    stages: DEFAULT_REVIEW_STAGES,
    basket_preview: makePreopenExecutionReviewBasket(),
    blocking_reasons: ["mvp_read_only"],
    warnings: ["not_in_current_preopen_contract"],
    notes: [
      "advisory_only",
      "no_live_execution",
      "mock_execution_requires_explicit_approval",
    ],
    ...overrides,
  };
}

export function makePreopenExecutionReviewUnavailable(
  overrides: Partial<ExecutionReviewSummary> = {},
): ExecutionReviewSummary {
  return makePreopenExecutionReview({
    readiness: {
      contract_version: "v1",
      account_mode: "db_simulated",
      execution_source: "preopen",
      is_ready: false,
      guard: {
        execution_allowed: false,
        approval_required: true,
        blocking_reasons: ["mvp_read_only", "no_open_preopen_run"],
        warnings: [],
      },
      checked_at: null,
      notes: [],
    },
    stages: DEFAULT_REVIEW_STAGES.map((stage) => ({
      ...stage,
      status: stage.stage_id === "approval_required" ? "pending" : "unavailable",
      summary:
        stage.stage_id === "approval_required"
          ? stage.summary
          : "No open preopen research run.",
    })),
    basket_preview: null,
    blocking_reasons: ["mvp_read_only", "no_open_preopen_run"],
    warnings: ["not_in_current_preopen_contract"],
    ...overrides,
  });
}
```

(c) Thread `execution_review` through the existing helpers. Find the return inside `makePreopenResponse` and add:

```typescript
    execution_review: makePreopenExecutionReview(),
```

right after `paper_approval_bridge: null,`.

Find the return inside `makePreopenFailOpen` and add:

```typescript
    execution_review: makePreopenExecutionReviewUnavailable(),
```

after `paper_approval_bridge: null,`.

- [ ] **Step 2: Run the existing frontend tests to confirm no regressions**

Run from `frontend/trading-decision/`:
```bash
npx vitest run
```
Expected: PASS (no test consumes the new field yet; the additional fixture data is harmless).

- [ ] **Step 3: Commit**

```bash
git add frontend/trading-decision/src/test/fixtures/preopen.ts
git commit -m "test(ROB-101): add execution review fixtures and thread through preopen helpers"
```

---

## Task 8: `ExecutionReviewPanel` component (TDD)

**Files:**
- Create: `frontend/trading-decision/src/components/ExecutionReviewPanel.tsx`
- Create: `frontend/trading-decision/src/components/ExecutionReviewPanel.module.css`
- Test: `frontend/trading-decision/src/__tests__/ExecutionReviewPanel.test.tsx` (new)

- [ ] **Step 1: Write the failing component test**

Create `frontend/trading-decision/src/__tests__/ExecutionReviewPanel.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ExecutionReviewPanel from "../components/ExecutionReviewPanel";
import {
  makePreopenExecutionReview,
  makePreopenExecutionReviewUnavailable,
} from "../test/fixtures/preopen";

describe("ExecutionReviewPanel", () => {
  it("renders nothing when review is null", () => {
    const { container } = render(<ExecutionReviewPanel review={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders guardrail banner with advisory copy and execution disabled", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    expect(
      screen.getByRole("region", { name: /execution review/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/advisory.*read[- ]only/i)).toBeInTheDocument();
    expect(screen.getByText(/no live execution/i)).toBeInTheDocument();
    expect(
      screen.getByText(/mock execution.*explicit approval/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Execution disabled/i)).toBeInTheDocument();
  });

  it("renders all six stages with their statuses", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    for (const label of [
      /data \/ news readiness/i,
      /candidate review/i,
      /cash \/ holdings \/ quotes/i,
      /basket preview/i,
      /approval required/i,
      /post-order reconciliation/i,
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText(/ready/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/unavailable/i).length).toBeGreaterThan(0);
  });

  it("renders basket preview lines when present", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    expect(screen.getByText("005930")).toBeInTheDocument();
    expect(screen.getByText(/buy/i)).toBeInTheDocument();
    expect(screen.getByText(/db_simulated/i)).toBeInTheDocument();
    expect(screen.getByText("70000")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    // Per-line guard rendered.
    expect(screen.getAllByText(/approval required/i).length).toBeGreaterThan(0);
  });

  it("hides basket preview block when basket is null and shows degraded copy", () => {
    render(
      <ExecutionReviewPanel review={makePreopenExecutionReviewUnavailable()} />,
    );

    expect(screen.getByText(/no open preopen research run/i)).toBeInTheDocument();
    expect(screen.queryByText("005930")).toBeNull();
  });

  it("renders blocking reasons as warning chips", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);
    expect(screen.getByText(/mvp_read_only/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run from `frontend/trading-decision/`:
```bash
npx vitest run src/__tests__/ExecutionReviewPanel.test.tsx
```
Expected: FAIL with "Cannot find module '../components/ExecutionReviewPanel'".

- [ ] **Step 3: Implement the component**

Create `frontend/trading-decision/src/components/ExecutionReviewPanel.module.css`:

```css
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--border-default, #2c3140);
  border-radius: 8px;
  background: var(--surface-default, #161922);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.headerTitle {
  margin: 0;
  font-size: 1.1rem;
}

.statusBadge {
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--surface-elevated, #232838);
  font-size: 0.85rem;
}

.guardrail {
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--surface-warning, rgba(255, 196, 0, 0.08));
  border: 1px solid var(--border-warning, rgba(255, 196, 0, 0.3));
  font-size: 0.9rem;
}

.stages {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.stage {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px;
  border: 1px solid var(--border-subtle, #232838);
  border-radius: 6px;
}

.stageStatus {
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.stageStatus.ready { color: var(--text-success, #4ade80); }
.stageStatus.degraded { color: var(--text-warning, #facc15); }
.stageStatus.unavailable { color: var(--text-muted, #94a3b8); }
.stageStatus.pending { color: var(--text-info, #60a5fa); }
.stageStatus.skipped { color: var(--text-muted, #94a3b8); }

.basketHeader {
  margin-top: 4px;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.basketLines {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.basketLine {
  display: grid;
  grid-template-columns: 1fr auto auto auto auto;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle, #232838);
  border-radius: 6px;
  align-items: center;
}

.warnings {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.warningChip {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--surface-elevated, #232838);
  font-size: 0.78rem;
}
```

Create `frontend/trading-decision/src/components/ExecutionReviewPanel.tsx`:

```tsx
import type {
  ExecutionReviewStage,
  ExecutionReviewSummary,
  OrderBasketPreview,
} from "../api/types";
import styles from "./ExecutionReviewPanel.module.css";

interface Props {
  review: ExecutionReviewSummary | null;
}

function StatusPill({ status }: { status: ExecutionReviewStage["status"] }) {
  return (
    <span className={`${styles.stageStatus} ${styles[status] ?? ""}`}>
      {status}
    </span>
  );
}

function BasketPreviewBlock({ basket }: { basket: OrderBasketPreview | null }) {
  if (!basket) return null;
  return (
    <div aria-label="Basket preview" role="group">
      <div className={styles.basketHeader}>
        <strong>Basket preview</strong>
        <span>
          {basket.account_mode} · {basket.lines.length} lines
        </span>
      </div>
      <ul className={styles.basketLines}>
        {basket.lines.map((line, idx) => (
          <li className={styles.basketLine} key={`${line.symbol}-${idx}`}>
            <span>
              <strong>{line.symbol}</strong>
              <span> · {line.market}</span>
            </span>
            <span>{line.side}</span>
            <span>{line.quantity ?? "—"}</span>
            <span>{line.limit_price ?? "—"}</span>
            <span>
              {line.guard.approval_required ? "Approval required" : "—"}
            </span>
          </li>
        ))}
      </ul>
      {basket.basket_warnings.length > 0 ? (
        <ul className={styles.warnings} aria-label="Basket warnings">
          {basket.basket_warnings.map((w) => (
            <li className={styles.warningChip} key={w}>
              {w}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export default function ExecutionReviewPanel({ review }: Props) {
  if (!review) return null;

  return (
    <section
      aria-label="Execution review"
      className={styles.panel}
    >
      <div className={styles.header}>
        <div>
          <h2 className={styles.headerTitle}>Execution review</h2>
          <p>
            Read-only stage view of preopen execution readiness. This page does
            not submit orders.
          </p>
        </div>
        <span className={styles.statusBadge}>Execution disabled</span>
      </div>

      <div className={styles.guardrail} role="note">
        <p>
          <strong>Advisory / read-only.</strong> No live execution. Mock
          execution requires later explicit operator approval.
        </p>
      </div>

      <ul className={styles.stages} aria-label="Execution review stages">
        {review.stages.map((stage) => (
          <li className={styles.stage} key={stage.stage_id}>
            <strong>{stage.label}</strong>
            <StatusPill status={stage.status} />
            <span>{stage.summary}</span>
            {stage.warnings.length > 0 ? (
              <ul
                className={styles.warnings}
                aria-label={`${stage.label} warnings`}
              >
                {stage.warnings.map((w) => (
                  <li className={styles.warningChip} key={w}>
                    {w}
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>

      <BasketPreviewBlock basket={review.basket_preview} />

      {review.blocking_reasons.length > 0 ? (
        <ul className={styles.warnings} aria-label="Execution blocking reasons">
          {review.blocking_reasons.map((reason) => (
            <li className={styles.warningChip} key={reason}>
              {reason}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
```

- [ ] **Step 4: Run the component test to verify it passes**

Run from `frontend/trading-decision/`:
```bash
npx vitest run src/__tests__/ExecutionReviewPanel.test.tsx
```
Expected: PASS for all six tests.

- [ ] **Step 5: Run the forbidden-imports safety test**

Run: `npx vitest run src/__tests__/forbidden_mutation_imports.test.ts`
Expected: PASS — the new component does not mention any forbidden mutation tokens.

- [ ] **Step 6: Commit**

```bash
git add frontend/trading-decision/src/components/ExecutionReviewPanel.tsx \
        frontend/trading-decision/src/components/ExecutionReviewPanel.module.css \
        frontend/trading-decision/src/__tests__/ExecutionReviewPanel.test.tsx
git commit -m "feat(ROB-101): add ExecutionReviewPanel component"
```

---

## Task 9: Wire `ExecutionReviewPanel` into `PreopenPage`

**Files:**
- Modify: `frontend/trading-decision/src/pages/PreopenPage.tsx`
- Modify: `frontend/trading-decision/src/__tests__/PreopenPage.test.tsx`

- [ ] **Step 1: Write the failing page test**

Append to `frontend/trading-decision/src/__tests__/PreopenPage.test.tsx`:

```tsx
  it("renders execution review panel with stage cards and basket preview when has_run", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /execution review/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Execution disabled/i)).toBeInTheDocument();
    expect(screen.getByText(/data \/ news readiness/i)).toBeInTheDocument();
    expect(screen.getByText(/basket preview/i)).toBeInTheDocument();
    expect(screen.getAllByText("005930").length).toBeGreaterThanOrEqual(2);
  });

  it("renders execution review panel with unavailable stages when no run", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenFailOpen())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /execution review/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/no_open_preopen_run/i)).toBeInTheDocument();
    expect(screen.getByText(/Execution disabled/i)).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run the failing page test**

Run from `frontend/trading-decision/`:
```bash
npx vitest run src/__tests__/PreopenPage.test.tsx
```
Expected: FAIL because `PreopenPage` does not yet render `ExecutionReviewPanel`.

- [ ] **Step 3: Wire the component into the page**

Edit `frontend/trading-decision/src/pages/PreopenPage.tsx`:

(a) Add to the imports:

```tsx
import ExecutionReviewPanel from "../components/ExecutionReviewPanel";
```

(b) In the no-run early-return JSX, render the panel after `<PreopenPaperApprovalBridgeSection .../>`:

```tsx
        <PreopenPaperApprovalBridgeSection bridge={data.paper_approval_bridge} />
        <ExecutionReviewPanel review={data.execution_review} />
```

(c) In the has-run JSX (the main `return (`), render the panel right after `<PreopenPaperApprovalBridgeSection .../>` (before `<NewsReadinessSection ... />`):

```tsx
      <PreopenPaperApprovalBridgeSection bridge={data.paper_approval_bridge} />
      <ExecutionReviewPanel review={data.execution_review} />
      <NewsReadinessSection news={data.news} preview={data.news_preview} />
```

- [ ] **Step 4: Run the page tests**

Run from `frontend/trading-decision/`:
```bash
npx vitest run src/__tests__/PreopenPage.test.tsx
```
Expected: PASS — both new test cases plus all pre-existing PreopenPage cases.

- [ ] **Step 5: Run the full frontend test suite**

```bash
npx vitest run
```
Expected: PASS across the suite.

- [ ] **Step 6: Run the typecheck**

```bash
npx tsc --noEmit
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/trading-decision/src/pages/PreopenPage.tsx \
        frontend/trading-decision/src/__tests__/PreopenPage.test.tsx
git commit -m "feat(ROB-101): render ExecutionReviewPanel on /preopen"
```

---

## Task 10: End-to-end verification — manual UI smoke + full test sweep

**Files:** none modified.

- [ ] **Step 1: Run all backend preopen tests**

Run:
```bash
uv run pytest tests/test_preopen_execution_review.py tests/test_router_preopen.py tests/test_preopen_dashboard_service.py tests/test_execution_contracts.py -v
```
Expected: PASS across all four files.

- [ ] **Step 2: Run linters**

Run:
```bash
make lint
```
Expected: PASS (Ruff + ty clean on the changed files).

- [ ] **Step 3: Run all frontend tests + typecheck**

Run from `frontend/trading-decision/`:
```bash
npx vitest run && npx tsc --noEmit
```
Expected: PASS.

- [ ] **Step 4: Manual UI smoke (browser)**

1. Start backend: `make dev`
2. Start frontend: `cd frontend/trading-decision && npm run dev`
3. Open `http://localhost:5173/preopen` (or whichever proxied URL the dev setup uses).
4. Confirm:
   - "Execution review" section renders.
   - Guardrail copy says "Advisory / read-only", "No live execution", "Mock execution requires later explicit operator approval".
   - "Execution disabled" badge is visible.
   - All six stages are visible (Data / news readiness, Candidate review, Cash / holdings / quotes, Basket preview, Approval required, Post-order reconciliation).
   - When a research run with buy candidates is loaded, the basket preview shows symbol / side / quantity / limit price rows.
   - When no run is loaded, the panel is still visible with stages set to `unavailable` and no basket rows.
5. Document: write 2 bullet points on what was verified into the PR description (golden path + degraded path).

If the manual UI step is not possible in the runtime environment, state explicitly in the PR description: "UI not manually verified — only Vitest + RTL coverage."

- [ ] **Step 5: Open the PR**

```bash
git push -u origin chipped-ounce
gh pr create --title "feat(ROB-101): preopen execution review panel and basket preview" --body "$(cat <<'EOF'
## Summary
- Adds an `execution_review` field to `/trading/api/preopen/latest` that reuses ROB-100 contract types (`ExecutionReadiness`, `ExecutionGuard`, `OrderPreviewLine`, `OrderBasketPreview`).
- Introduces `ExecutionReviewPanel` on `/preopen` with six stage chips, an "Execution disabled" guardrail, and an optional basket preview rendered from buy candidates.
- Read-only MVP: conservative defaults (`execution_allowed=false`, `approval_required=true`, `is_ready=false`), no broker / order / watch / intent imports, no live or mock submit.

## Test plan
- [ ] `uv run pytest tests/test_preopen_execution_review.py tests/test_router_preopen.py tests/test_preopen_dashboard_service.py -v`
- [ ] `make lint`
- [ ] `npx vitest run` (all suites green)
- [ ] `npx tsc --noEmit`
- [ ] Manual browser smoke on `/preopen` (golden + no-run path)
EOF
)"
```

(Skip this step if the operator wants to push manually or the worktree is shared.)

---

## Spec coverage check

| Spec requirement | Covered by |
|---|---|
| Wire `ExecutionReadiness` etc. into preopen response using shared contract | Tasks 1, 2, 4 |
| Expose degraded/unavailable for cash, holdings, quotes, broker/order, watch | Task 2 (`cash_holdings_quotes` stage; `mvp_read_only` blocking on readiness) |
| Expose optional basket preview when present | Task 2 (basket built from buy candidates) |
| Keep existing API compatibility | Task 1 step 5 (existing tests still pass; field is optional) |
| Add execution review/workflow panel to `/preopen` | Tasks 8–9 |
| Stage status: data/news readiness, candidate review, cash/holdings/quotes, basket preview, approval required, post-order reconcile | Task 2 stages (matching IDs) |
| Render basket preview cards if provided | Task 8 (`BasketPreviewBlock`) |
| Guardrail copy: advisory/read-only, no live execution, mock requires explicit approval | Task 8 (guardrail banner + tests) |
| Backend/frontend targeted tests pass | Tasks 1–9, Task 10 sweep |
| Reuse `app.schemas.execution_contracts` (no new local vocabulary) | Tasks 1–2 imports |
| Conservative `execution_allowed=false`, `approval_required=true` | Tasks 1–2 (literal `False` / `True` in schema) |
| Read-only MVP, no submit/intent side effects | Task 5 forbidden-imports test |
