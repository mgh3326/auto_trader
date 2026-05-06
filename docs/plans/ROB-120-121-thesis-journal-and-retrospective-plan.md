# ROB-120 / ROB-121 — Position Thesis Journal & Research Retrospective Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Implementer = Sonnet, planner/reviewer = Opus, one implementer editing at a time.

**Goal:**
- **ROB-120** — Add an operator-facing Position Thesis Journal page at `/trading/decisions/journal` that surfaces, per holding, whether a thesis exists (target/stop/min hold/Research Session link) and lets the operator create or edit a `draft`/`active` journal via explicit form submit. No broker/order side effects.
- **ROB-121** — Add a read-only Research Retrospective page at `/trading/decisions/retrospective` that aggregates research stage coverage, AI-vs-user decision distribution, stale/unavailable signal coverage, and realized/unrealized PnL across `trading_decision_outcomes`, with drill-down links into existing Research Session and order-outcome detail pages.

**Architecture:**
- **ROB-120** reuses the existing `review.trade_journals` table + `TradeJournal` ORM and the existing `save_trade_journal` MCP path; we only add (a) a *coverage* aggregator that joins live + manual + Upbit holdings against the latest active/draft journal per symbol, (b) a thin write-through HTTP API restricted to `status ∈ {draft, active}` with no `trade_id`/`exit_*`/`pnl_*` mutation, and (c) a React page. ROB-120 makes **no** schema changes; "linked research_session_id / research_summary_id" is recorded as `extra_metadata.research_session_id` / `extra_metadata.research_summary_id` (JSONB on the existing column) so we don't grow the table for a feature that can ship without it.
- **ROB-121** is purely a read-only aggregation service joining `research_sessions`, `research_summaries`, `summary_stage_links`, `stage_analysis`, `user_research_notes`, `trading_decision_sessions`, `trading_decision_proposals`, `trading_decision_outcomes`. Three GET endpoints (`overview`, `stage-performance`, `decisions`) feed one page with filters (period / market / strategy). No schema changes. No scheduler changes. No outcome backfill.
- Both pages live in the existing `frontend/trading-decision` SPA (Vite + React 19 + react-router-dom v7) and follow the pattern set by `PortfolioActionsPage` / `CandidatesPage` (ROB-116/117): typed `api/` module → page that fetches once on filter change → CSS module styling → Korean i18n strings under `i18n/ko.ts`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Pydantic v2, Alembic (no new migrations needed), Vite + React 19 + TypeScript, react-router-dom v7, vitest + @testing-library, pytest + pytest-asyncio.

---

## 0. Scope, non-goals, and ordering

**ROB-121 blocks ROB-120 in Linear by error**: The Linear data shows `ROB-121` is *blocked by* ROB-120 (and ROB-116/117/118/119), not the other way around. Implementation order in this plan: **Part A (ROB-120) first → Part B (ROB-121) second**, matching the dependency direction. Each part is independently shippable as its own PR.

**Both issues — hard non-goals (do not violate, no exceptions):**
- No broker/order/Alpaca/KIS/Upbit mutation. No `dry_run=False` paths. No order placement, cancellation, or modification.
- No direct DB `INSERT`/`UPDATE`/`DELETE` outside of (a) the new `TradeJournalWriteService` for ROB-120 and (b) zero writes for ROB-121 (read-only aggregation).
- No automatic backfill of existing journals or outcomes. Coverage gaps surface as UI empty states and warnings — never as silent inserts.
- No scheduler / cron / taskiq changes.
- No Alembic migration. If you find yourself reaching for one, stop and re-read this section.
- No `extra_metadata` write that includes anything beyond `research_session_id` / `research_summary_id` (both `int | None`); other keys are out of scope for this PR.

**Symbol normalization:** The journal page and retrospective page both display equity tickers; for US equities convert via `app.core.symbol.to_db_symbol` before querying and via the symbol as-stored when displaying. Crypto symbols stay in `KRW-BTC` form as already used elsewhere.

---

## 1. File inventory

### 1.1 ROB-120 — Position Thesis Journal

**Backend — create**

- `app/schemas/trade_journal.py` — Pydantic v2 DTOs: `JournalCoverageRow`, `JournalCoverageResponse`, `JournalCreateRequest`, `JournalUpdateRequest`, `JournalReadResponse`.
- `app/services/trade_journal_coverage_service.py` — `TradeJournalCoverageService` reads-only aggregator that joins holdings (Merged + Upbit) against the latest non-`closed`/non-`stopped`/non-`expired` journal per `(symbol, account_type)` plus the latest `ResearchSummary.session_id` for that symbol.
- `app/services/trade_journal_write_service.py` — `TradeJournalWriteService` with `create_draft_or_active(...)` and `update_draft_or_active(...)`. Both reject any payload that mutates `trade_id`, `exit_price`, `exit_date`, `exit_reason`, `pnl_pct`, `paper_trade_id`, `account_type=='paper'` without `account`, or `status ∈ {closed, stopped, expired}`.
- `app/routers/trade_journals.py` — FastAPI router under `/trading/api/trade-journals` with: `GET /coverage`, `GET /by-symbol/{symbol}`, `POST /` (create draft/active), `PUT /{journal_id}` (update draft/active).

**Backend — modify**

- `app/main.py` — register the new router (one line near `portfolio_actions.router`).

**Backend — tests (create)**

- `tests/services/test_trade_journal_coverage_service.py`
- `tests/services/test_trade_journal_write_service.py`
- `tests/routers/test_trade_journals_router.py`
- `tests/schemas/test_trade_journal_schemas.py`

**Frontend — create**

- `frontend/trading-decision/src/api/tradeJournals.ts` — typed wrappers around the four endpoints.
- `frontend/trading-decision/src/pages/JournalPage.tsx`
- `frontend/trading-decision/src/pages/JournalPage.module.css`
- `frontend/trading-decision/src/components/JournalCoverageRow.tsx`
- `frontend/trading-decision/src/components/JournalCoverageRow.module.css`
- `frontend/trading-decision/src/components/JournalEditDialog.tsx`
- `frontend/trading-decision/src/components/JournalEditDialog.module.css`
- `frontend/trading-decision/src/__tests__/JournalPage.test.tsx`
- `frontend/trading-decision/src/__tests__/JournalEditDialog.test.tsx`

**Frontend — modify**

- `frontend/trading-decision/src/api/types.ts` — add `JournalCoverageRow`, `JournalCoverageResponse`, `JournalReadResponse`, `JournalCreateRequest`, `JournalUpdateRequest`, `JournalStatus` (union literal).
- `frontend/trading-decision/src/routes.tsx` — add `{ path: "/journal", element: <JournalPage /> }` and the import.
- `frontend/trading-decision/src/i18n/ko.ts` — add a `journal` namespace with all UI labels.

### 1.2 ROB-121 — Research Retrospective

**Backend — create**

- `app/schemas/research_retrospective.py` — Pydantic v2 DTOs: `RetrospectiveOverview`, `StagePerformanceRow`, `RetrospectiveDecisionRow`, `RetrospectiveDecisionsResponse`.
- `app/services/research_retrospective_service.py` — `ResearchRetrospectiveService` with `build_overview(...)`, `build_stage_performance(...)`, `list_decisions(...)`. Read-only; no transactions.
- `app/routers/research_retrospective.py` — FastAPI router with three GETs.

**Backend — modify**

- `app/main.py` — register the new router.

**Backend — tests (create)**

- `tests/services/test_research_retrospective_service.py`
- `tests/routers/test_research_retrospective_router.py`

**Frontend — create**

- `frontend/trading-decision/src/api/researchRetrospective.ts`
- `frontend/trading-decision/src/pages/RetrospectivePage.tsx`
- `frontend/trading-decision/src/pages/RetrospectivePage.module.css`
- `frontend/trading-decision/src/components/RetrospectiveStagePerformance.tsx`
- `frontend/trading-decision/src/components/RetrospectiveDecisionTable.tsx`
- `frontend/trading-decision/src/__tests__/RetrospectivePage.test.tsx`

**Frontend — modify**

- `frontend/trading-decision/src/api/types.ts` — add retrospective types.
- `frontend/trading-decision/src/routes.tsx` — add `{ path: "/retrospective", element: <RetrospectivePage /> }`.
- `frontend/trading-decision/src/i18n/ko.ts` — add a `retrospective` namespace.

---

# Part A — ROB-120: Position Thesis Journal

## A1. Backend schemas (Pydantic DTOs)

**Files:**
- Create: `app/schemas/trade_journal.py`
- Test: `tests/schemas/test_trade_journal_schemas.py`

- [ ] **Step 1: Write the failing schema tests**

```python
# tests/schemas/test_trade_journal_schemas.py
import pytest
from pydantic import ValidationError

from app.schemas.trade_journal import (
    JournalCoverageResponse,
    JournalCoverageRow,
    JournalCreateRequest,
    JournalReadResponse,
    JournalUpdateRequest,
)


def test_create_request_rejects_closed_status() -> None:
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="meaningful thesis",
            status="closed",
        )


def test_create_request_accepts_draft_and_active() -> None:
    for status in ("draft", "active"):
        req = JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="meaningful thesis",
            status=status,
        )
        assert req.status == status


def test_create_request_rejects_negative_min_hold_days() -> None:
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="thesis",
            min_hold_days=-1,
        )


def test_create_request_rejects_empty_thesis() -> None:
    with pytest.raises(ValidationError):
        JournalCreateRequest(
            symbol="005930",
            instrument_type="equity_kr",
            thesis="   ",
        )


def test_update_request_allows_partial_payload() -> None:
    req = JournalUpdateRequest(thesis="updated thesis")
    assert req.thesis == "updated thesis"
    assert req.target_price is None


def test_update_request_rejects_terminal_status() -> None:
    with pytest.raises(ValidationError):
        JournalUpdateRequest(status="stopped")


def test_coverage_response_round_trip() -> None:
    row = JournalCoverageRow(
        symbol="005930",
        name="삼성전자",
        market="KR",
        instrument_type="equity_kr",
        quantity=10.0,
        position_weight_pct=12.5,
        journal_status="missing",
        journal_id=None,
        thesis=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        hold_until=None,
        latest_research_session_id=None,
        latest_research_summary_id=None,
        latest_summary_decision=None,
        thesis_conflict_with_summary=False,
    )
    resp = JournalCoverageResponse(generated_at="2026-05-06T00:00:00Z", total=1, rows=[row])
    assert resp.total == 1


def test_read_response_fields_present() -> None:
    JournalReadResponse(
        id=1,
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        thesis="t",
        status="draft",
        account_type="live",
        created_at="2026-05-06T00:00:00Z",
        updated_at="2026-05-06T00:00:00Z",
        research_session_id=None,
        research_summary_id=None,
    )
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/schemas/test_trade_journal_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.trade_journal'`.

- [ ] **Step 3: Implement the schemas**

```python
# app/schemas/trade_journal.py
"""ROB-120 — Position thesis journal DTOs (operator-facing read + write)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JournalStatus = Literal["draft", "active", "closed", "stopped", "expired"]
WritableJournalStatus = Literal["draft", "active"]
JournalCoverageStatus = Literal["present", "missing", "stale"]
SummaryDecision = Literal["buy", "hold", "sell"]
Market = Literal["KR", "US", "CRYPTO"]


class JournalCoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = None
    market: Market
    instrument_type: str | None = None
    quantity: float | None = None
    position_weight_pct: float | None = None

    journal_status: JournalCoverageStatus = "missing"
    journal_id: int | None = None
    thesis: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = None
    hold_until: str | None = None

    latest_research_session_id: int | None = None
    latest_research_summary_id: int | None = None
    latest_summary_decision: SummaryDecision | None = None
    thesis_conflict_with_summary: bool = False


class JournalCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    total: int
    rows: list[JournalCoverageRow]
    warnings: list[str] = Field(default_factory=list)


class JournalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    instrument_type: str
    side: Literal["buy", "sell"] = "buy"
    thesis: str
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = Field(default=None, ge=0, le=3650)
    status: WritableJournalStatus = "draft"
    account: str | None = None
    notes: str | None = None
    research_session_id: int | None = None
    research_summary_id: int | None = None

    @field_validator("thesis")
    @classmethod
    def thesis_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("thesis must not be blank")
        return v

    @field_validator("symbol")
    @classmethod
    def symbol_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("symbol must not be blank")
        return v


class JournalUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: str | None = None
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = Field(default=None, ge=0, le=3650)
    status: WritableJournalStatus | None = None
    notes: str | None = None
    research_session_id: int | None = None
    research_summary_id: int | None = None

    @field_validator("thesis")
    @classmethod
    def thesis_not_blank_when_present(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("thesis must not be blank when provided")
        return v


class JournalReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    symbol: str
    instrument_type: str
    side: Literal["buy", "sell"]
    thesis: str
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    min_hold_days: int | None = None
    hold_until: str | None = None
    status: JournalStatus
    account: str | None = None
    account_type: Literal["live", "paper"]
    notes: str | None = None
    research_session_id: int | None = None
    research_summary_id: int | None = None
    created_at: str
    updated_at: str
```

- [ ] **Step 4: Re-run schema tests, expect green**

Run: `uv run pytest tests/schemas/test_trade_journal_schemas.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/trade_journal.py tests/schemas/test_trade_journal_schemas.py
git commit -m "feat(ROB-120): add trade journal DTOs"
```

## A2. Backend — `TradeJournalWriteService`

**Files:**
- Create: `app/services/trade_journal_write_service.py`
- Test: `tests/services/test_trade_journal_write_service.py`

- [ ] **Step 1: Write the failing service tests**

```python
# tests/services/test_trade_journal_write_service.py
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.trade_journal import TradeJournal
from app.schemas.trade_journal import JournalCreateRequest, JournalUpdateRequest
from app.services.trade_journal_write_service import (
    JournalWriteError,
    TradeJournalWriteService,
)


@pytest.mark.asyncio
async def test_create_inserts_draft_journal_with_research_metadata(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    req = JournalCreateRequest(
        symbol="005930",
        instrument_type="equity_kr",
        thesis="long-term semis play",
        target_price=80000.0,
        stop_loss=60000.0,
        min_hold_days=30,
        research_session_id=42,
        research_summary_id=7,
    )

    created = await svc.create(req)

    assert created.id is not None
    assert created.status == "draft"
    row = (
        await db_session.execute(
            select(TradeJournal).where(TradeJournal.id == created.id)
        )
    ).scalar_one()
    assert row.thesis == "long-term semis play"
    assert row.extra_metadata == {"research_session_id": 42, "research_summary_id": 7}
    assert row.hold_until is not None
    assert row.hold_until - datetime.now(UTC) > timedelta(days=29)


@pytest.mark.asyncio
async def test_create_paper_without_account_raises(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    req = JournalCreateRequest(
        symbol="005930",
        instrument_type="equity_kr",
        thesis="t",
        # account_type defaults to live in DB; service forces account when account_type=paper
    )
    with pytest.raises(ValidationError):
        # account_type isn't on the create DTO — service is live-only by design
        JournalCreateRequest(  # type: ignore[arg-type]
            symbol="005930",
            instrument_type="equity_kr",
            thesis="t",
            account_type="paper",
        )


@pytest.mark.asyncio
async def test_update_rejects_terminal_status(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    created = await svc.create(
        JournalCreateRequest(
            symbol="005930", instrument_type="equity_kr", thesis="t"
        )
    )
    with pytest.raises(ValidationError):
        JournalUpdateRequest(status="closed")  # DTO blocks
    with pytest.raises(JournalWriteError):
        # service-level guard for forged payloads
        await svc._apply_update(  # noqa: SLF001
            created.id, {"status": "stopped"}
        )


@pytest.mark.asyncio
async def test_update_modifies_thesis_and_research_metadata(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    created = await svc.create(
        JournalCreateRequest(symbol="005930", instrument_type="equity_kr", thesis="t1")
    )
    updated = await svc.update(
        created.id,
        JournalUpdateRequest(thesis="t2", research_session_id=99),
    )
    assert updated.thesis == "t2"
    assert updated.research_session_id == 99


@pytest.mark.asyncio
async def test_update_missing_id_raises(db_session) -> None:
    svc = TradeJournalWriteService(db_session)
    with pytest.raises(JournalWriteError):
        await svc.update(99999, JournalUpdateRequest(thesis="x"))
```

- [ ] **Step 2: Confirm fail**

Run: `uv run pytest tests/services/test_trade_journal_write_service.py -v`
Expected: collection error / `ModuleNotFoundError`.

- [ ] **Step 3: Implement the service**

```python
# app/services/trade_journal_write_service.py
"""ROB-120 — Write-through service for the operator-facing thesis journal.

Hard rules:
  * Only `live` account journals are created or updated here. Paper journals
    are created by the existing paper-trade journal pipeline.
  * Status is restricted to {draft, active}. Terminal transitions
    (closed, stopped, expired) and exit_* / pnl_* fields are owned by
    downstream services and never mutated here.
  * `extra_metadata` is rewritten with the merge of any pre-existing keys
    plus (`research_session_id`, `research_summary_id`) — no other keys.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.schemas.trade_journal import (
    JournalCreateRequest,
    JournalReadResponse,
    JournalUpdateRequest,
)

_WRITABLE_STATUSES = frozenset({"draft", "active"})


class JournalWriteError(Exception):
    """Raised when a payload would mutate forbidden fields."""


def _to_read(j: TradeJournal) -> JournalReadResponse:
    meta = j.extra_metadata or {}
    return JournalReadResponse(
        id=j.id,
        symbol=j.symbol,
        instrument_type=j.instrument_type.value
        if hasattr(j.instrument_type, "value")
        else str(j.instrument_type),
        side=j.side,  # type: ignore[arg-type]
        thesis=j.thesis,
        strategy=j.strategy,
        target_price=float(j.target_price) if j.target_price is not None else None,
        stop_loss=float(j.stop_loss) if j.stop_loss is not None else None,
        min_hold_days=j.min_hold_days,
        hold_until=j.hold_until.isoformat() if j.hold_until else None,
        status=j.status,  # type: ignore[arg-type]
        account=j.account,
        account_type=j.account_type,  # type: ignore[arg-type]
        notes=j.notes,
        research_session_id=meta.get("research_session_id")
        if isinstance(meta, dict)
        else None,
        research_summary_id=meta.get("research_summary_id")
        if isinstance(meta, dict)
        else None,
        created_at=j.created_at.isoformat(),
        updated_at=j.updated_at.isoformat(),
    )


def _coerce_instrument_type(raw: str) -> InstrumentType:
    try:
        return InstrumentType(raw)
    except ValueError as exc:
        raise JournalWriteError(f"invalid instrument_type: {raw}") from exc


def _build_metadata(
    existing: dict[str, Any] | None,
    research_session_id: int | None,
    research_summary_id: int | None,
) -> dict[str, Any] | None:
    out: dict[str, Any] = dict(existing or {})
    if research_session_id is not None:
        out["research_session_id"] = research_session_id
    if research_summary_id is not None:
        out["research_summary_id"] = research_summary_id
    return out or None


class TradeJournalWriteService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, req: JournalCreateRequest) -> JournalReadResponse:
        if req.status not in _WRITABLE_STATUSES:
            raise JournalWriteError(f"status {req.status!r} not writable")
        hold_until: datetime | None = None
        if req.min_hold_days is not None:
            hold_until = datetime.now(UTC) + timedelta(days=req.min_hold_days)
        journal = TradeJournal(
            symbol=req.symbol.strip(),
            instrument_type=_coerce_instrument_type(req.instrument_type),
            side=req.side,
            thesis=req.thesis,
            strategy=req.strategy,
            target_price=req.target_price,
            stop_loss=req.stop_loss,
            min_hold_days=req.min_hold_days,
            hold_until=hold_until,
            status=req.status,
            account=req.account,
            account_type="live",
            notes=req.notes,
            extra_metadata=_build_metadata(
                None, req.research_session_id, req.research_summary_id
            ),
        )
        self.db.add(journal)
        await self.db.flush()
        await self.db.refresh(journal)
        return _to_read(journal)

    async def update(
        self, journal_id: int, req: JournalUpdateRequest
    ) -> JournalReadResponse:
        payload: dict[str, Any] = req.model_dump(exclude_none=True)
        return await self._apply_update(journal_id, payload)

    async def _apply_update(
        self, journal_id: int, payload: dict[str, Any]
    ) -> JournalReadResponse:
        if "status" in payload and payload["status"] not in _WRITABLE_STATUSES:
            raise JournalWriteError(
                f"refusing to update status to {payload['status']!r}"
            )
        # Belt-and-suspenders: forbidden columns must never be touched here.
        forbidden = {
            "trade_id",
            "exit_price",
            "exit_date",
            "exit_reason",
            "pnl_pct",
            "paper_trade_id",
            "account_type",
        }
        offending = forbidden & payload.keys()
        if offending:
            raise JournalWriteError(
                f"refusing to mutate forbidden fields: {sorted(offending)}"
            )

        row = (
            await self.db.execute(
                select(TradeJournal).where(TradeJournal.id == journal_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise JournalWriteError(f"journal {journal_id} not found")
        if row.account_type != "live":
            raise JournalWriteError("paper journals are not editable here")

        research_session_id = payload.pop("research_session_id", None)
        research_summary_id = payload.pop("research_summary_id", None)

        for key, value in payload.items():
            if hasattr(row, key):
                setattr(row, key, value)

        if "min_hold_days" in payload and payload["min_hold_days"] is not None:
            row.hold_until = datetime.now(UTC) + timedelta(
                days=payload["min_hold_days"]
            )

        if research_session_id is not None or research_summary_id is not None:
            row.extra_metadata = _build_metadata(
                row.extra_metadata, research_session_id, research_summary_id
            )

        await self.db.flush()
        await self.db.refresh(row)
        return _to_read(row)
```

- [ ] **Step 4: Wire `db_session` fixture if missing**

Check `tests/conftest.py` for an existing `db_session` async fixture (the repo has one for service tests). If absent, add one alongside other DB fixtures — do **not** invent a new fixture file. If the fixture already exists, this step is a no-op; commit nothing for it.

- [ ] **Step 5: Run service tests**

Run: `uv run pytest tests/services/test_trade_journal_write_service.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/trade_journal_write_service.py tests/services/test_trade_journal_write_service.py
git commit -m "feat(ROB-120): add TradeJournalWriteService for draft/active journals"
```

## A3. Backend — `TradeJournalCoverageService`

**Files:**
- Create: `app/services/trade_journal_coverage_service.py`
- Test: `tests/services/test_trade_journal_coverage_service.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_trade_journal_coverage_service.py
import pytest

from app.services.trade_journal_coverage_service import TradeJournalCoverageService


@pytest.mark.asyncio
async def test_holding_with_active_journal_is_present(
    db_session, seed_holding_005930, seed_active_journal_005930
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=seed_holding_005930.user_id)
    rows = {r.symbol: r for r in resp.rows}
    assert rows["005930"].journal_status == "present"
    assert rows["005930"].thesis is not None


@pytest.mark.asyncio
async def test_holding_without_journal_is_missing(
    db_session, seed_holding_aapl
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=seed_holding_aapl.user_id)
    assert resp.rows[0].symbol == "AAPL"
    assert resp.rows[0].journal_status == "missing"
    assert resp.rows[0].thesis is None


@pytest.mark.asyncio
async def test_market_filter_restricts_results(
    db_session, seed_holding_005930, seed_holding_aapl
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(
        user_id=seed_holding_005930.user_id, market_filter="KR"
    )
    assert {r.symbol for r in resp.rows} == {"005930"}


@pytest.mark.asyncio
async def test_thesis_conflict_when_summary_decision_is_sell_and_journal_active(
    db_session, seed_holding_005930, seed_active_journal_005930, seed_summary_sell_005930
) -> None:
    svc = TradeJournalCoverageService(db_session)
    resp = await svc.build_coverage(user_id=seed_holding_005930.user_id)
    row = resp.rows[0]
    assert row.thesis_conflict_with_summary is True
    assert row.latest_summary_decision == "sell"
```

> **Fixtures note:** the four fixtures (`seed_holding_005930`, `seed_holding_aapl`, `seed_active_journal_005930`, `seed_summary_sell_005930`) live in `tests/conftest.py` alongside other holdings/research seed fixtures. Implement them by directly inserting `manual_holdings` / `trade_journals` / `research_sessions` + `research_summaries` rows the same way `tests/services/test_portfolio_action_service.py` already seeds holdings; keep the new fixtures in the same file as that test if there is no shared conftest. Do **not** mock the DB.

- [ ] **Step 2: Confirm fail**

Run: `uv run pytest tests/services/test_trade_journal_coverage_service.py -v`
Expected: collection error.

- [ ] **Step 3: Implement the service**

```python
# app/services/trade_journal_coverage_service.py
"""ROB-120 — Read-only coverage aggregator for the thesis journal page.

Joins (live + manual + Upbit) holdings against the latest open journal
per (symbol, account_type='live') and the latest research_summary for
that symbol's stock_info row. Produces one row per holding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_pipeline import ResearchSession, ResearchSummary
from app.models.trade_journal import TradeJournal
from app.schemas.trade_journal import (
    JournalCoverageResponse,
    JournalCoverageRow,
)
from app.services.merged_portfolio_service import MergedPortfolioService

_OPEN_STATUSES = ("draft", "active")


class TradeJournalCoverageService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_coverage(
        self,
        *,
        user_id: int,
        market_filter: str | None = None,
    ) -> JournalCoverageResponse:
        merged = MergedPortfolioService(self.db)
        holdings = await merged.list_user_holdings(user_id=user_id)

        if market_filter:
            holdings = [
                h
                for h in holdings
                if str(getattr(getattr(h, "market_type", None), "value", "")) == market_filter
            ]

        total_value = float(sum((getattr(h, "evaluation", 0.0) or 0.0) for h in holdings))
        rows: list[JournalCoverageRow] = []
        warnings: list[str] = []

        for h in holdings:
            quantity = float(getattr(h, "quantity", 0.0) or 0.0)
            if quantity <= 0:
                continue
            symbol = str(getattr(h, "ticker", "") or "")
            if not symbol:
                continue

            evaluation = float(getattr(h, "evaluation", 0.0) or 0.0)
            weight = (evaluation / total_value * 100.0) if total_value else None
            market_value = str(getattr(getattr(h, "market_type", None), "value", "KR"))

            journal = await self._latest_open_journal(symbol)
            summary_row = await self._latest_summary_for_symbol(symbol)

            journal_status = "present" if journal is not None else "missing"
            decision: str | None = None
            session_id: int | None = None
            summary_id: int | None = None
            if summary_row is not None:
                summary_id, session_id, decision = summary_row

            conflict = bool(
                journal is not None
                and journal.status == "active"
                and decision == "sell"
            )

            meta: dict[str, Any] | None = (
                journal.extra_metadata if journal is not None else None
            )
            row_session_id = (
                meta.get("research_session_id")
                if isinstance(meta, dict)
                else None
            )
            row_summary_id = (
                meta.get("research_summary_id")
                if isinstance(meta, dict)
                else None
            )

            rows.append(
                JournalCoverageRow(
                    symbol=symbol,
                    name=getattr(h, "name", None),
                    market=market_value,  # type: ignore[arg-type]
                    instrument_type=getattr(h, "instrument_type", None),
                    quantity=quantity,
                    position_weight_pct=weight,
                    journal_status=journal_status,  # type: ignore[arg-type]
                    journal_id=journal.id if journal else None,
                    thesis=journal.thesis if journal else None,
                    target_price=float(journal.target_price)
                    if journal and journal.target_price is not None
                    else None,
                    stop_loss=float(journal.stop_loss)
                    if journal and journal.stop_loss is not None
                    else None,
                    min_hold_days=journal.min_hold_days if journal else None,
                    hold_until=journal.hold_until.isoformat()
                    if journal and journal.hold_until
                    else None,
                    latest_research_session_id=row_session_id or session_id,
                    latest_research_summary_id=row_summary_id or summary_id,
                    latest_summary_decision=decision,  # type: ignore[arg-type]
                    thesis_conflict_with_summary=conflict,
                )
            )

        return JournalCoverageResponse(
            generated_at=datetime.now(UTC).isoformat(),
            total=len(rows),
            rows=rows,
            warnings=warnings,
        )

    async def _latest_open_journal(self, symbol: str) -> TradeJournal | None:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.symbol == symbol,
                TradeJournal.account_type == "live",
                TradeJournal.status.in_(_OPEN_STATUSES),
            )
            .order_by(desc(TradeJournal.created_at))
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _latest_summary_for_symbol(
        self, symbol: str
    ) -> tuple[int, int, str] | None:
        # research_summaries → research_sessions → stock_info.symbol
        from app.models.analysis import StockInfo  # local import to avoid cycle

        stmt = (
            select(ResearchSummary.id, ResearchSession.id, ResearchSummary.decision)
            .join(ResearchSession, ResearchSummary.session_id == ResearchSession.id)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .where(StockInfo.symbol == symbol)
            .order_by(desc(ResearchSummary.executed_at))
            .limit(1)
        )
        row = (await self.db.execute(stmt)).first()
        if row is None:
            return None
        return (int(row[0]), int(row[1]), str(row[2]))
```

- [ ] **Step 4: Run coverage tests**

Run: `uv run pytest tests/services/test_trade_journal_coverage_service.py -v`
Expected: all pass. If `MergedPortfolioService.list_user_holdings` differs from the assumed signature, fix the call site to match the existing service contract — do **not** add a wrapper.

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal_coverage_service.py tests/services/test_trade_journal_coverage_service.py
git commit -m "feat(ROB-120): add TradeJournalCoverageService aggregator"
```

## A4. Backend — Router

**Files:**
- Create: `app/routers/trade_journals.py`
- Modify: `app/main.py`
- Test: `tests/routers/test_trade_journals_router.py`

- [ ] **Step 1: Write the failing router tests**

```python
# tests/routers/test_trade_journals_router.py
import pytest


@pytest.mark.asyncio
async def test_get_coverage_requires_auth(client_unauth) -> None:
    res = await client_unauth.get("/trading/api/trade-journals/coverage")
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_coverage_returns_rows(client_with_user, seed_holding_005930) -> None:
    res = await client_with_user.get("/trading/api/trade-journals/coverage")
    assert res.status_code == 200
    body = res.json()
    assert "rows" in body and isinstance(body["rows"], list)
    assert body["total"] == len(body["rows"])


@pytest.mark.asyncio
async def test_post_creates_draft_journal(client_with_user) -> None:
    res = await client_with_user.post(
        "/trading/api/trade-journals/",
        json={
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "thesis": "long-term play",
            "status": "draft",
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "draft"
    assert body["thesis"] == "long-term play"


@pytest.mark.asyncio
async def test_post_rejects_closed_status(client_with_user) -> None:
    res = await client_with_user.post(
        "/trading/api/trade-journals/",
        json={
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "thesis": "t",
            "status": "closed",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_put_updates_thesis(client_with_user) -> None:
    created = (
        await client_with_user.post(
            "/trading/api/trade-journals/",
            json={"symbol": "005930", "instrument_type": "equity_kr", "thesis": "a"},
        )
    ).json()
    res = await client_with_user.put(
        f"/trading/api/trade-journals/{created['id']}",
        json={"thesis": "b"},
    )
    assert res.status_code == 200
    assert res.json()["thesis"] == "b"


@pytest.mark.asyncio
async def test_put_rejects_terminal_status(client_with_user) -> None:
    created = (
        await client_with_user.post(
            "/trading/api/trade-journals/",
            json={"symbol": "005930", "instrument_type": "equity_kr", "thesis": "a"},
        )
    ).json()
    res = await client_with_user.put(
        f"/trading/api/trade-journals/{created['id']}",
        json={"status": "stopped"},
    )
    assert res.status_code == 422
```

> **Fixtures `client_with_user` / `client_unauth`** already exist in `tests/conftest.py` and are used by `tests/routers/test_portfolio_actions.py`. Reuse them.

- [ ] **Step 2: Confirm fail**

Run: `uv run pytest tests/routers/test_trade_journals_router.py -v`
Expected: 404s (router not registered).

- [ ] **Step 3: Implement the router**

```python
# app/routers/trade_journals.py
"""ROB-120 — Position thesis journal router (live-only, draft/active write)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trade_journal import TradeJournal
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.trade_journal import (
    JournalCoverageResponse,
    JournalCreateRequest,
    JournalReadResponse,
    JournalUpdateRequest,
)
from app.services.trade_journal_coverage_service import TradeJournalCoverageService
from app.services.trade_journal_write_service import (
    JournalWriteError,
    TradeJournalWriteService,
    _to_read,
)
from sqlalchemy import desc, select

api_router = APIRouter(prefix="/api/trade-journals", tags=["trade-journals"])
router = APIRouter()


def _coverage_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TradeJournalCoverageService:
    return TradeJournalCoverageService(db)


def _write_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TradeJournalWriteService:
    return TradeJournalWriteService(db)


@api_router.get("/coverage", response_model=JournalCoverageResponse)
async def get_coverage(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    svc: Annotated[TradeJournalCoverageService, Depends(_coverage_service)],
    market: Annotated[
        Literal["KR", "US", "CRYPTO"] | None,
        Query(description="Optional market filter"),
    ] = None,
) -> JournalCoverageResponse:
    return await svc.build_coverage(user_id=current_user.id, market_filter=market)


@api_router.get("/by-symbol/{symbol}", response_model=list[JournalReadResponse])
async def list_by_symbol(
    symbol: Annotated[str, Path(min_length=1)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[JournalReadResponse]:
    rows = (
        await db.execute(
            select(TradeJournal)
            .where(TradeJournal.symbol == symbol, TradeJournal.account_type == "live")
            .order_by(desc(TradeJournal.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return [_to_read(j) for j in rows]


@api_router.post(
    "/",
    response_model=JournalReadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_journal(
    payload: JournalCreateRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    svc: Annotated[TradeJournalWriteService, Depends(_write_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JournalReadResponse:
    try:
        result = await svc.create(payload)
    except JournalWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return result


@api_router.put("/{journal_id}", response_model=JournalReadResponse)
async def update_journal(
    journal_id: Annotated[int, Path(ge=1)],
    payload: JournalUpdateRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    svc: Annotated[TradeJournalWriteService, Depends(_write_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JournalReadResponse:
    try:
        result = await svc.update(journal_id, payload)
    except JournalWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return result


router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
```

- [ ] **Step 4: Register the router in `app/main.py`**

Add the import next to other router imports and the registration line near `portfolio_actions.router`:

```python
# top imports
from app.routers import trade_journals

# inside the route registration block, after portfolio_actions.router:
app.include_router(trade_journals.router)
```

- [ ] **Step 5: Run router tests**

Run: `uv run pytest tests/routers/test_trade_journals_router.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/routers/trade_journals.py app/main.py tests/routers/test_trade_journals_router.py
git commit -m "feat(ROB-120): add /trading/api/trade-journals router"
```

## A5. Frontend — types, API wrapper, route

**Files:**
- Modify: `frontend/trading-decision/src/api/types.ts`
- Create: `frontend/trading-decision/src/api/tradeJournals.ts`
- Modify: `frontend/trading-decision/src/routes.tsx`

- [ ] **Step 1: Add types**

Append to `frontend/trading-decision/src/api/types.ts`:

```ts
export type JournalStatus = "draft" | "active" | "closed" | "stopped" | "expired";
export type WritableJournalStatus = "draft" | "active";
export type JournalCoverageStatus = "present" | "missing" | "stale";
export type SummaryDecision = "buy" | "hold" | "sell";

export interface JournalCoverageRow {
  symbol: string;
  name: string | null;
  market: Market;
  instrument_type: string | null;
  quantity: number | null;
  position_weight_pct: number | null;
  journal_status: JournalCoverageStatus;
  journal_id: number | null;
  thesis: string | null;
  target_price: number | null;
  stop_loss: number | null;
  min_hold_days: number | null;
  hold_until: string | null;
  latest_research_session_id: number | null;
  latest_research_summary_id: number | null;
  latest_summary_decision: SummaryDecision | null;
  thesis_conflict_with_summary: boolean;
}

export interface JournalCoverageResponse {
  generated_at: string;
  total: number;
  rows: JournalCoverageRow[];
  warnings: string[];
}

export interface JournalReadResponse {
  id: number;
  symbol: string;
  instrument_type: string;
  side: "buy" | "sell";
  thesis: string;
  strategy: string | null;
  target_price: number | null;
  stop_loss: number | null;
  min_hold_days: number | null;
  hold_until: string | null;
  status: JournalStatus;
  account: string | null;
  account_type: "live" | "paper";
  notes: string | null;
  research_session_id: number | null;
  research_summary_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface JournalCreateRequest {
  symbol: string;
  instrument_type: string;
  side?: "buy" | "sell";
  thesis: string;
  strategy?: string | null;
  target_price?: number | null;
  stop_loss?: number | null;
  min_hold_days?: number | null;
  status?: WritableJournalStatus;
  account?: string | null;
  notes?: string | null;
  research_session_id?: number | null;
  research_summary_id?: number | null;
}

export interface JournalUpdateRequest {
  thesis?: string;
  strategy?: string | null;
  target_price?: number | null;
  stop_loss?: number | null;
  min_hold_days?: number | null;
  status?: WritableJournalStatus;
  notes?: string | null;
  research_session_id?: number | null;
  research_summary_id?: number | null;
}
```

- [ ] **Step 2: Create the API wrapper**

```ts
// frontend/trading-decision/src/api/tradeJournals.ts
import { apiFetch } from "./client";
import type {
  JournalCoverageResponse,
  JournalCreateRequest,
  JournalReadResponse,
  JournalUpdateRequest,
  Market,
} from "./types";

export function getJournalCoverage(
  market?: Market,
): Promise<JournalCoverageResponse> {
  const qs = market ? `?market=${encodeURIComponent(market)}` : "";
  return apiFetch<JournalCoverageResponse>(`/trade-journals/coverage${qs}`);
}

export function listJournalsBySymbol(
  symbol: string,
  limit = 10,
): Promise<JournalReadResponse[]> {
  const qs = `?limit=${limit}`;
  return apiFetch<JournalReadResponse[]>(
    `/trade-journals/by-symbol/${encodeURIComponent(symbol)}${qs}`,
  );
}

export function createJournal(
  body: JournalCreateRequest,
): Promise<JournalReadResponse> {
  return apiFetch<JournalReadResponse>(`/trade-journals/`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateJournal(
  id: number,
  body: JournalUpdateRequest,
): Promise<JournalReadResponse> {
  return apiFetch<JournalReadResponse>(`/trade-journals/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
```

- [ ] **Step 3: Add the route**

In `frontend/trading-decision/src/routes.tsx`, add the import and route entry:

```tsx
import JournalPage from "./pages/JournalPage";
// ... existing imports ...

export const tradingDecisionRoutes: RouteObject[] = [
  // ... existing routes ...
  { path: "/portfolio-actions", element: <PortfolioActionsPage /> },
  { path: "/candidates", element: <CandidatesPage /> },
  { path: "/journal", element: <JournalPage /> },
  // ... rest ...
];
```

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/api/types.ts frontend/trading-decision/src/api/tradeJournals.ts frontend/trading-decision/src/routes.tsx
git commit -m "feat(ROB-120): add trade journal API client and route"
```

## A6. Frontend — i18n strings

**Files:**
- Modify: `frontend/trading-decision/src/i18n/ko.ts`

- [ ] **Step 1: Add the `journal` namespace**

Append to `i18n/ko.ts` (alongside `portfolioActions`):

```ts
export const journal = {
  pageTitle: "Position Thesis Journal",
  pageSubtitle: "보유 종목별 thesis / 목표가 / 손절가 / 보유 기간 정리",
  filterMarket: "시장",
  filterAll: "전체",
  marketKR: "KR",
  marketUS: "US",
  marketCRYPTO: "암호화폐",
  loadError: "Journal coverage 로딩 실패",
  empty: "보유 종목이 없습니다.",
  status: { present: "Thesis 있음", missing: "Thesis 없음", stale: "Stale" },
  conflictBadge: "최신 Research Summary와 thesis 충돌",
  ctaCreate: "Thesis 작성",
  ctaEdit: "수정",
  ctaOpenSession: "Research Session 열기",
  table: {
    symbol: "종목",
    market: "시장",
    quantity: "수량",
    weight: "비중",
    journalStatus: "상태",
    thesis: "Thesis",
    target: "목표가",
    stop: "손절가",
    minHold: "최소 보유일",
    summaryDecision: "최신 결정",
    actions: "액션",
  },
  dialog: {
    titleCreate: "Thesis 작성",
    titleEdit: "Thesis 수정",
    fieldThesis: "Thesis (필수)",
    fieldStrategy: "전략",
    fieldTarget: "목표가",
    fieldStop: "손절가",
    fieldMinHold: "최소 보유일",
    fieldStatus: "상태",
    fieldNotes: "메모",
    fieldResearchSession: "Research Session ID",
    fieldResearchSummary: "Research Summary ID",
    submit: "저장",
    cancel: "취소",
    saveError: "저장 실패",
    statusDraft: "draft",
    statusActive: "active",
  },
};
```

- [ ] **Step 2: Commit**

```bash
git add frontend/trading-decision/src/i18n/ko.ts
git commit -m "feat(ROB-120): add Korean i18n for journal page"
```

## A7. Frontend — `JournalCoverageRow` component

**Files:**
- Create: `frontend/trading-decision/src/components/JournalCoverageRow.tsx`
- Create: `frontend/trading-decision/src/components/JournalCoverageRow.module.css`

- [ ] **Step 1: Implement the row**

```tsx
// frontend/trading-decision/src/components/JournalCoverageRow.tsx
import type { JournalCoverageRow as Row } from "../api/types";
import { journal as t } from "../i18n/ko";
import styles from "./JournalCoverageRow.module.css";

interface Props {
  row: Row;
  onCreate: (row: Row) => void;
  onEdit: (row: Row) => void;
}

function fmt(n: number | null, digits = 2): string {
  return n === null || Number.isNaN(n) ? "-" : n.toFixed(digits);
}

export default function JournalCoverageRow({ row, onCreate, onEdit }: Props) {
  const isMissing = row.journal_status === "missing";
  return (
    <tr className={styles.row}>
      <td>
        <div className={styles.symbol}>{row.symbol}</div>
        {row.name ? <div className={styles.name}>{row.name}</div> : null}
      </td>
      <td>{row.market}</td>
      <td className={styles.numeric}>{fmt(row.quantity, 4)}</td>
      <td className={styles.numeric}>{fmt(row.position_weight_pct, 1)}%</td>
      <td>
        <span
          className={`${styles.badge} ${
            isMissing ? styles.badgeMissing : styles.badgePresent
          }`}
        >
          {t.status[row.journal_status]}
        </span>
        {row.thesis_conflict_with_summary ? (
          <span className={styles.conflict}>{t.conflictBadge}</span>
        ) : null}
      </td>
      <td className={styles.thesis}>{row.thesis ?? "-"}</td>
      <td className={styles.numeric}>{fmt(row.target_price)}</td>
      <td className={styles.numeric}>{fmt(row.stop_loss)}</td>
      <td className={styles.numeric}>{row.min_hold_days ?? "-"}</td>
      <td>{row.latest_summary_decision ?? "-"}</td>
      <td className={styles.actions}>
        {isMissing ? (
          <button type="button" onClick={() => onCreate(row)}>
            {t.ctaCreate}
          </button>
        ) : (
          <button type="button" onClick={() => onEdit(row)}>
            {t.ctaEdit}
          </button>
        )}
        {row.latest_research_session_id !== null ? (
          <a
            className={styles.link}
            href={`/trading/decisions/research/sessions/${row.latest_research_session_id}/summary`}
          >
            {t.ctaOpenSession}
          </a>
        ) : null}
      </td>
    </tr>
  );
}
```

```css
/* frontend/trading-decision/src/components/JournalCoverageRow.module.css */
.row td {
  padding: 8px 12px;
  border-bottom: 1px solid #eee;
  vertical-align: top;
}
.symbol { font-weight: 600; }
.name { color: #666; font-size: 12px; }
.numeric { text-align: right; font-variant-numeric: tabular-nums; }
.thesis { max-width: 320px; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px;
}
.badgeMissing { background: #fde2e2; color: #b91c1c; }
.badgePresent { background: #e0f2fe; color: #075985; }
.conflict {
  display: inline-block; margin-left: 6px; padding: 2px 8px;
  border-radius: 4px; font-size: 12px; background: #fef3c7; color: #92400e;
}
.actions { display: flex; gap: 8px; align-items: center; }
.link { font-size: 12px; }
```

- [ ] **Step 2: Commit**

```bash
git add frontend/trading-decision/src/components/JournalCoverageRow.tsx frontend/trading-decision/src/components/JournalCoverageRow.module.css
git commit -m "feat(ROB-120): add JournalCoverageRow component"
```

## A8. Frontend — `JournalEditDialog` (form + tests)

**Files:**
- Create: `frontend/trading-decision/src/components/JournalEditDialog.tsx`
- Create: `frontend/trading-decision/src/components/JournalEditDialog.module.css`
- Create: `frontend/trading-decision/src/__tests__/JournalEditDialog.test.tsx`

- [ ] **Step 1: Write the failing dialog test**

```tsx
// frontend/trading-decision/src/__tests__/JournalEditDialog.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, cleanup } from "@testing-library/react";

import JournalEditDialog from "../components/JournalEditDialog";

afterEach(() => cleanup());

describe("JournalEditDialog", () => {
  it("requires a non-empty thesis on submit", () => {
    const onSubmit = vi.fn();
    render(
      <JournalEditDialog
        mode="create"
        symbol="005930"
        instrumentType="equity_kr"
        initial={null}
        onCancel={() => {}}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /저장/ }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits with parsed numeric fields", () => {
    const onSubmit = vi.fn();
    render(
      <JournalEditDialog
        mode="create"
        symbol="005930"
        instrumentType="equity_kr"
        initial={null}
        onCancel={() => {}}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Thesis/), {
      target: { value: "long" },
    });
    fireEvent.change(screen.getByLabelText(/목표가/), {
      target: { value: "80000" },
    });
    fireEvent.click(screen.getByRole("button", { name: /저장/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toMatchObject({
      thesis: "long",
      target_price: 80000,
    });
  });
});
```

- [ ] **Step 2: Confirm fail**

Run: `cd frontend/trading-decision && npm test -- JournalEditDialog`
Expected: import error.

- [ ] **Step 3: Implement the dialog**

```tsx
// frontend/trading-decision/src/components/JournalEditDialog.tsx
import { useState } from "react";

import type {
  JournalCreateRequest,
  JournalReadResponse,
  JournalUpdateRequest,
  WritableJournalStatus,
} from "../api/types";
import { journal as t } from "../i18n/ko";
import styles from "./JournalEditDialog.module.css";

type Mode = "create" | "edit";

interface Props {
  mode: Mode;
  symbol: string;
  instrumentType: string;
  initial: JournalReadResponse | null;
  onCancel: () => void;
  onSubmit: (payload: JournalCreateRequest | JournalUpdateRequest) => void;
}

function toNumber(s: string): number | null {
  if (s.trim() === "") return null;
  const v = Number(s);
  return Number.isFinite(v) ? v : null;
}

export default function JournalEditDialog({
  mode,
  symbol,
  instrumentType,
  initial,
  onCancel,
  onSubmit,
}: Props) {
  const [thesis, setThesis] = useState(initial?.thesis ?? "");
  const [strategy, setStrategy] = useState(initial?.strategy ?? "");
  const [target, setTarget] = useState(
    initial?.target_price !== null && initial?.target_price !== undefined
      ? String(initial.target_price)
      : "",
  );
  const [stop, setStop] = useState(
    initial?.stop_loss !== null && initial?.stop_loss !== undefined
      ? String(initial.stop_loss)
      : "",
  );
  const [minHold, setMinHold] = useState(
    initial?.min_hold_days !== null && initial?.min_hold_days !== undefined
      ? String(initial.min_hold_days)
      : "",
  );
  const [status, setStatus] = useState<WritableJournalStatus>(
    (initial?.status === "active" ? "active" : "draft"),
  );
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [sessionId, setSessionId] = useState(
    initial?.research_session_id !== null && initial?.research_session_id !== undefined
      ? String(initial.research_session_id)
      : "",
  );
  const [summaryId, setSummaryId] = useState(
    initial?.research_summary_id !== null && initial?.research_summary_id !== undefined
      ? String(initial.research_summary_id)
      : "",
  );
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!thesis.trim()) {
      setError(t.dialog.fieldThesis);
      return;
    }
    const base = {
      thesis: thesis.trim(),
      strategy: strategy.trim() || null,
      target_price: toNumber(target),
      stop_loss: toNumber(stop),
      min_hold_days: minHold.trim() === "" ? null : Number(minHold),
      status,
      notes: notes.trim() || null,
      research_session_id: sessionId.trim() === "" ? null : Number(sessionId),
      research_summary_id: summaryId.trim() === "" ? null : Number(summaryId),
    };
    if (mode === "create") {
      onSubmit({
        symbol,
        instrument_type: instrumentType,
        side: "buy",
        ...base,
      } satisfies JournalCreateRequest);
    } else {
      onSubmit(base satisfies JournalUpdateRequest);
    }
  };

  return (
    <div className={styles.backdrop} role="dialog" aria-modal>
      <form className={styles.dialog} onSubmit={handleSubmit}>
        <h2>{mode === "create" ? t.dialog.titleCreate : t.dialog.titleEdit}</h2>
        <p className={styles.symbol}>{symbol}</p>

        <label>
          {t.dialog.fieldThesis}
          <textarea
            value={thesis}
            onChange={(e) => setThesis(e.target.value)}
            rows={4}
            required
          />
        </label>
        <label>
          {t.dialog.fieldStrategy}
          <input value={strategy} onChange={(e) => setStrategy(e.target.value)} />
        </label>
        <div className={styles.grid}>
          <label>
            {t.dialog.fieldTarget}
            <input
              type="number"
              step="any"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            />
          </label>
          <label>
            {t.dialog.fieldStop}
            <input
              type="number"
              step="any"
              value={stop}
              onChange={(e) => setStop(e.target.value)}
            />
          </label>
          <label>
            {t.dialog.fieldMinHold}
            <input
              type="number"
              min={0}
              max={3650}
              value={minHold}
              onChange={(e) => setMinHold(e.target.value)}
            />
          </label>
          <label>
            {t.dialog.fieldStatus}
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value as WritableJournalStatus)}
            >
              <option value="draft">{t.dialog.statusDraft}</option>
              <option value="active">{t.dialog.statusActive}</option>
            </select>
          </label>
          <label>
            {t.dialog.fieldResearchSession}
            <input
              type="number"
              min={0}
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
            />
          </label>
          <label>
            {t.dialog.fieldResearchSummary}
            <input
              type="number"
              min={0}
              value={summaryId}
              onChange={(e) => setSummaryId(e.target.value)}
            />
          </label>
        </div>
        <label>
          {t.dialog.fieldNotes}
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
          />
        </label>

        {error ? <p className={styles.error}>{error}</p> : null}

        <div className={styles.buttons}>
          <button type="button" onClick={onCancel}>
            {t.dialog.cancel}
          </button>
          <button type="submit">{t.dialog.submit}</button>
        </div>
      </form>
    </div>
  );
}
```

```css
/* frontend/trading-decision/src/components/JournalEditDialog.module.css */
.backdrop {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.dialog {
  background: white; padding: 24px; min-width: 440px; max-width: 640px;
  border-radius: 8px; display: flex; flex-direction: column; gap: 12px;
}
.symbol { color: #666; margin: 0; font-family: monospace; }
.grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
}
.error { color: #b91c1c; margin: 0; }
.buttons { display: flex; justify-content: flex-end; gap: 8px; }
```

- [ ] **Step 4: Run dialog tests**

Run: `cd frontend/trading-decision && npm test -- JournalEditDialog`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/components/JournalEditDialog.tsx frontend/trading-decision/src/components/JournalEditDialog.module.css frontend/trading-decision/src/__tests__/JournalEditDialog.test.tsx
git commit -m "feat(ROB-120): add JournalEditDialog form"
```

## A9. Frontend — `JournalPage`

**Files:**
- Create: `frontend/trading-decision/src/pages/JournalPage.tsx`
- Create: `frontend/trading-decision/src/pages/JournalPage.module.css`
- Create: `frontend/trading-decision/src/__tests__/JournalPage.test.tsx`

- [ ] **Step 1: Write the failing page test**

```tsx
// frontend/trading-decision/src/__tests__/JournalPage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api/tradeJournals", () => ({
  getJournalCoverage: vi.fn(),
  listJournalsBySymbol: vi.fn(),
  createJournal: vi.fn(),
  updateJournal: vi.fn(),
}));

import * as api from "../api/tradeJournals";
import JournalPage from "../pages/JournalPage";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

beforeEach(() => {
  (api.getJournalCoverage as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    generated_at: "2026-05-06T00:00:00Z",
    total: 1,
    rows: [
      {
        symbol: "005930",
        name: "삼성전자",
        market: "KR",
        instrument_type: "equity_kr",
        quantity: 10,
        position_weight_pct: 12.5,
        journal_status: "missing",
        journal_id: null,
        thesis: null,
        target_price: null,
        stop_loss: null,
        min_hold_days: null,
        hold_until: null,
        latest_research_session_id: null,
        latest_research_summary_id: null,
        latest_summary_decision: null,
        thesis_conflict_with_summary: false,
      },
    ],
    warnings: [],
  });
});

describe("JournalPage", () => {
  it("renders coverage rows", async () => {
    render(<JournalPage />);
    await waitFor(() => {
      expect(screen.getByText("005930")).toBeInTheDocument();
    });
    expect(screen.getByText("Thesis 없음")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Confirm fail**

Run: `cd frontend/trading-decision && npm test -- JournalPage`
Expected: import error.

- [ ] **Step 3: Implement the page**

```tsx
// frontend/trading-decision/src/pages/JournalPage.tsx
import { useCallback, useEffect, useMemo, useState } from "react";

import JournalCoverageRow from "../components/JournalCoverageRow";
import JournalEditDialog from "../components/JournalEditDialog";
import {
  createJournal,
  getJournalCoverage,
  listJournalsBySymbol,
  updateJournal,
} from "../api/tradeJournals";
import type {
  JournalCoverageResponse,
  JournalCoverageRow as RowT,
  JournalCreateRequest,
  JournalReadResponse,
  JournalUpdateRequest,
  Market,
} from "../api/types";
import { journal as t } from "../i18n/ko";
import styles from "./JournalPage.module.css";

type DialogState =
  | { mode: "create"; row: RowT; initial: null }
  | { mode: "edit"; row: RowT; initial: JournalReadResponse }
  | null;

export default function JournalPage() {
  const [data, setData] = useState<JournalCoverageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [market, setMarket] = useState<Market | "ALL">("ALL");
  const [dialog, setDialog] = useState<DialogState>(null);

  const reload = useCallback(() => {
    setError(null);
    getJournalCoverage(market === "ALL" ? undefined : market)
      .then((res) => setData(res))
      .catch(() => setError(t.loadError));
  }, [market]);

  useEffect(() => {
    reload();
  }, [reload]);

  const onCreate = useCallback(
    (row: RowT) => setDialog({ mode: "create", row, initial: null }),
    [],
  );

  const onEdit = useCallback(async (row: RowT) => {
    if (row.journal_id === null) return;
    const list = await listJournalsBySymbol(row.symbol, 1);
    const initial = list[0] ?? null;
    if (initial) setDialog({ mode: "edit", row, initial });
  }, []);

  const handleSubmit = useCallback(
    async (payload: JournalCreateRequest | JournalUpdateRequest) => {
      if (dialog === null) return;
      try {
        if (dialog.mode === "create") {
          await createJournal(payload as JournalCreateRequest);
        } else {
          await updateJournal(
            dialog.initial.id,
            payload as JournalUpdateRequest,
          );
        }
        setDialog(null);
        reload();
      } catch {
        setError(t.dialog.saveError);
      }
    },
    [dialog, reload],
  );

  const rows = useMemo<RowT[]>(() => data?.rows ?? [], [data]);

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>{t.pageTitle}</h1>
        <p>{t.pageSubtitle}</p>
      </header>

      <div className={styles.filters}>
        <label>
          {t.filterMarket}
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as Market | "ALL")}
          >
            <option value="ALL">{t.filterAll}</option>
            <option value="KR">{t.marketKR}</option>
            <option value="US">{t.marketUS}</option>
            <option value="CRYPTO">{t.marketCRYPTO}</option>
          </select>
        </label>
      </div>

      {error ? <p className={styles.error}>{error}</p> : null}

      {rows.length === 0 ? (
        <p className={styles.empty}>{t.empty}</p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>{t.table.symbol}</th>
              <th>{t.table.market}</th>
              <th>{t.table.quantity}</th>
              <th>{t.table.weight}</th>
              <th>{t.table.journalStatus}</th>
              <th>{t.table.thesis}</th>
              <th>{t.table.target}</th>
              <th>{t.table.stop}</th>
              <th>{t.table.minHold}</th>
              <th>{t.table.summaryDecision}</th>
              <th>{t.table.actions}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <JournalCoverageRow
                key={row.symbol}
                row={row}
                onCreate={onCreate}
                onEdit={onEdit}
              />
            ))}
          </tbody>
        </table>
      )}

      {dialog ? (
        <JournalEditDialog
          mode={dialog.mode}
          symbol={dialog.row.symbol}
          instrumentType={dialog.row.instrument_type ?? "equity_kr"}
          initial={dialog.initial}
          onCancel={() => setDialog(null)}
          onSubmit={handleSubmit}
        />
      ) : null}
    </main>
  );
}
```

```css
/* frontend/trading-decision/src/pages/JournalPage.module.css */
.page { padding: 24px; }
.header h1 { margin: 0 0 4px; }
.header p { margin: 0; color: #555; }
.filters { display: flex; gap: 16px; margin: 16px 0; }
.filters label { display: flex; flex-direction: column; font-size: 13px; }
.error { color: #b91c1c; }
.empty { color: #555; padding: 24px 0; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td {
  text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee;
  font-size: 14px;
}
```

- [ ] **Step 4: Run page test**

Run: `cd frontend/trading-decision && npm test -- JournalPage`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/pages/JournalPage.tsx frontend/trading-decision/src/pages/JournalPage.module.css frontend/trading-decision/src/__tests__/JournalPage.test.tsx
git commit -m "feat(ROB-120): add JournalPage with edit dialog"
```

## A10. End-to-end checks for ROB-120

- [ ] **Step 1: Backend type/lint/tests**

Run:
```bash
make lint
uv run pytest tests/schemas/test_trade_journal_schemas.py tests/services/test_trade_journal_write_service.py tests/services/test_trade_journal_coverage_service.py tests/routers/test_trade_journals_router.py -v
```
Expected: lint clean, all journal tests pass.

- [ ] **Step 2: Frontend typecheck/test/build**

Run:
```bash
cd frontend/trading-decision
npm run typecheck
npm test
npm run build
```
Expected: typecheck clean, all journal tests pass, vite build succeeds.

- [ ] **Step 3: Smoke the page locally**

Start `make dev`, visit `http://localhost:8000/trading/decisions/journal`, confirm:
- coverage rows render with correct status badges
- "Thesis 작성" creates a draft journal (verify in DB the `extra_metadata` JSON contains the research IDs you submitted)
- "수정" opens the dialog pre-filled and the update persists
- attempting `status=closed` via curl returns 422 (manual check)

If any item fails, fix in place — do not move to ROB-121 until journal page is operational.

- [ ] **Step 4: Final ROB-120 commit + PR**

```bash
git push -u origin feature/ROB-120-position-thesis-journal
gh pr create --base main --title "feat(ROB-120): Position Thesis Journal page" --body "Closes ROB-120. ..."
```

---

# Part B — ROB-121: Research Retrospective

> Begin Part B in a separate branch (`feature/ROB-121-research-retrospective`) once Part A's PR is open. Part B does not import any Part A symbols, so the branches do not block each other beyond shared `routes.tsx` and `i18n/ko.ts` edits — rebase Part B on top of Part A's merged state to avoid conflicts.

## B1. Backend — DTOs

**Files:**
- Create: `app/schemas/research_retrospective.py`
- Test: extend `tests/services/test_research_retrospective_service.py` (next task)

- [ ] **Step 1: Implement DTOs**

```python
# app/schemas/research_retrospective.py
"""ROB-121 — Research retrospective aggregation DTOs (read-only)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Market = Literal["KR", "US", "CRYPTO"]
DecisionVerdict = Literal["buy", "hold", "sell"]
StageType = Literal["market", "news", "fundamentals", "social"]


class StageCoverageStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_type: StageType
    coverage_pct: float = Field(ge=0.0, le=100.0)
    stale_pct: float = Field(ge=0.0, le=100.0)
    unavailable_pct: float = Field(ge=0.0, le=100.0)


class DecisionDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_buy: int
    ai_hold: int
    ai_sell: int
    user_accept: int
    user_reject: int
    user_modify: int
    user_defer: int
    user_pending: int


class PnlSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    realized_pnl_pct_avg: float | None = None
    unrealized_pnl_pct_avg: float | None = None
    sample_size: int


class RetrospectiveOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: str
    period_end: str
    market: Market | None
    strategy: str | None
    sessions_total: int
    summaries_total: int
    decision_distribution: DecisionDistribution
    stage_coverage: list[StageCoverageStat]
    pnl: PnlSummary
    warnings: list[str] = Field(default_factory=list)


class StagePerformanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_combo: str  # e.g. "market+news+fundamentals", "market+news"
    sample_size: int
    win_rate_pct: float | None = None  # share with realized_pnl_pct > 0
    avg_realized_pnl_pct: float | None = None


class RetrospectiveDecisionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_session_id: int
    symbol: str
    market: Market
    decided_at: str
    ai_decision: DecisionVerdict | None = None
    user_response: str | None = None
    realized_pnl_pct: float | None = None
    unrealized_pnl_pct_7d: float | None = None
    proposal_id: int | None = None


class RetrospectiveDecisionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    rows: list[RetrospectiveDecisionRow]
    next_cursor: str | None = None
```

- [ ] **Step 2: Commit**

```bash
git add app/schemas/research_retrospective.py
git commit -m "feat(ROB-121): add research retrospective DTOs"
```

## B2. Backend — `ResearchRetrospectiveService`

**Files:**
- Create: `app/services/research_retrospective_service.py`
- Test: `tests/services/test_research_retrospective_service.py`

- [ ] **Step 1: Write failing service tests**

```python
# tests/services/test_research_retrospective_service.py
from datetime import UTC, datetime, timedelta

import pytest

from app.schemas.research_retrospective import (
    RetrospectiveDecisionsResponse,
    RetrospectiveOverview,
)
from app.services.research_retrospective_service import (
    ResearchRetrospectiveService,
)


@pytest.mark.asyncio
async def test_overview_empty_when_no_data(db_session) -> None:
    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC)
    out = await svc.build_overview(
        period_start=end - timedelta(days=30),
        period_end=end,
        market=None,
        strategy=None,
    )
    assert isinstance(out, RetrospectiveOverview)
    assert out.sessions_total == 0
    assert out.summaries_total == 0
    assert "no_research_summaries_in_window" in out.warnings


@pytest.mark.asyncio
async def test_overview_counts_summaries_in_window(
    db_session, seed_summary_buy_005930, seed_summary_sell_aapl
) -> None:
    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC)
    out = await svc.build_overview(
        period_start=end - timedelta(days=30),
        period_end=end,
        market=None,
        strategy=None,
    )
    assert out.sessions_total == 2
    assert out.summaries_total == 2
    assert out.decision_distribution.ai_buy == 1
    assert out.decision_distribution.ai_sell == 1


@pytest.mark.asyncio
async def test_overview_market_filter(
    db_session, seed_summary_buy_005930, seed_summary_sell_aapl
) -> None:
    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC)
    out = await svc.build_overview(
        period_start=end - timedelta(days=30),
        period_end=end,
        market="KR",
        strategy=None,
    )
    assert out.sessions_total == 1
    assert out.decision_distribution.ai_buy == 1
    assert out.decision_distribution.ai_sell == 0


@pytest.mark.asyncio
async def test_stage_performance_groups_by_combo(
    db_session,
    seed_summary_with_market_news_005930,
    seed_summary_with_market_only_aapl,
) -> None:
    svc = ResearchRetrospectiveService(db_session)
    rows = await svc.build_stage_performance(
        period_start=datetime.now(UTC) - timedelta(days=30),
        period_end=datetime.now(UTC),
        market=None,
        strategy=None,
    )
    combos = {r.stage_combo for r in rows}
    assert "market+news" in combos
    assert "market" in combos


@pytest.mark.asyncio
async def test_list_decisions_returns_drilldown_rows(
    db_session, seed_decision_session_with_outcome_005930
) -> None:
    svc = ResearchRetrospectiveService(db_session)
    out = await svc.list_decisions(
        period_start=datetime.now(UTC) - timedelta(days=30),
        period_end=datetime.now(UTC),
        market=None,
        strategy=None,
        limit=20,
        cursor=None,
    )
    assert isinstance(out, RetrospectiveDecisionsResponse)
    assert out.total >= 1
    assert out.rows[0].research_session_id is not None
    assert out.rows[0].proposal_id is not None
```

> **Fixtures:** seed minimal `research_sessions` + `research_summaries` (+ optional `stage_analysis`, `trading_decision_sessions`, `trading_decision_proposals`, `trading_decision_outcomes`) the same way the rest of the test suite seeds them. Place fixtures in `tests/conftest.py` if shared, or local to the file otherwise.

- [ ] **Step 2: Confirm fail**

Run: `uv run pytest tests/services/test_research_retrospective_service.py -v`

- [ ] **Step 3: Implement the service**

```python
# app/services/research_retrospective_service.py
"""ROB-121 — Read-only aggregation service for the retrospective page.

NEVER writes to the database. NEVER triggers brokers / scheduler / outbox.
Aggregates over (research_summaries, summary_stage_links, stage_analysis,
trading_decision_proposals, trading_decision_outcomes).

`market` filter maps to stock_info.instrument_type:
  KR -> equity_kr
  US -> equity_us
  CRYPTO -> crypto
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockInfo
from app.models.research_pipeline import (
    ResearchSession,
    ResearchSummary,
    StageAnalysis,
    SummaryStageLink,
)
from app.models.trading_decision import (
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.schemas.research_retrospective import (
    DecisionDistribution,
    Market,
    PnlSummary,
    RetrospectiveDecisionRow,
    RetrospectiveDecisionsResponse,
    RetrospectiveOverview,
    StageCoverageStat,
    StagePerformanceRow,
)

_MARKET_TO_INSTRUMENT = {
    "KR": "equity_kr",
    "US": "equity_us",
    "CRYPTO": "crypto",
}


class ResearchRetrospectiveService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_overview(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
        strategy: str | None,
    ) -> RetrospectiveOverview:
        warnings: list[str] = []

        summary_rows = (
            await self.db.execute(self._summary_query(period_start, period_end, market))
        ).all()

        summaries_total = len(summary_rows)
        sessions_total = len({row.session_id for row in summary_rows})
        decision_counts = Counter(row.decision for row in summary_rows)

        if summaries_total == 0:
            warnings.append("no_research_summaries_in_window")

        distribution = DecisionDistribution(
            ai_buy=decision_counts.get("buy", 0),
            ai_hold=decision_counts.get("hold", 0),
            ai_sell=decision_counts.get("sell", 0),
            user_accept=0,
            user_reject=0,
            user_modify=0,
            user_defer=0,
            user_pending=0,
        )

        proposal_responses = await self._proposal_response_counts(
            period_start, period_end, market
        )
        for key, count in proposal_responses.items():
            field = f"user_{key}"
            if hasattr(distribution, field):
                setattr(distribution, field, count)

        stage_stats = await self._stage_coverage(period_start, period_end, market)
        pnl = await self._pnl_summary(period_start, period_end, market)

        return RetrospectiveOverview(
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            market=market,
            strategy=strategy,
            sessions_total=sessions_total,
            summaries_total=summaries_total,
            decision_distribution=distribution,
            stage_coverage=stage_stats,
            pnl=pnl,
            warnings=warnings,
        )

    async def build_stage_performance(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
        strategy: str | None,
    ) -> list[StagePerformanceRow]:
        del strategy  # not yet used; reserved for future strategy_name filter
        rows = (
            await self.db.execute(self._summary_query(period_start, period_end, market))
        ).all()
        if not rows:
            return []

        summary_ids = [row.summary_id for row in rows]
        link_rows = (
            await self.db.execute(
                select(
                    SummaryStageLink.summary_id, StageAnalysis.stage_type
                )
                .join(StageAnalysis, SummaryStageLink.stage_analysis_id == StageAnalysis.id)
                .where(SummaryStageLink.summary_id.in_(summary_ids))
            )
        ).all()

        combos: dict[int, set[str]] = {}
        for sid, stage in link_rows:
            combos.setdefault(int(sid), set()).add(str(stage))

        outcomes = await self._outcomes_by_proposal(period_start, period_end, market)

        groups: dict[str, list[float]] = {}
        for row in rows:
            stages = sorted(combos.get(row.summary_id, set()))
            key = "+".join(stages) if stages else "no_stages"
            pnls = outcomes.get(row.session_id, [])
            groups.setdefault(key, []).extend(pnls)

        out: list[StagePerformanceRow] = []
        for combo, pnls in groups.items():
            sample = len(pnls)
            win_rate = (
                sum(1 for v in pnls if v > 0) / sample * 100.0 if sample else None
            )
            avg = sum(pnls) / sample if sample else None
            out.append(
                StagePerformanceRow(
                    stage_combo=combo,
                    sample_size=sample,
                    win_rate_pct=win_rate,
                    avg_realized_pnl_pct=avg,
                )
            )
        out.sort(key=lambda r: -r.sample_size)
        return out

    async def list_decisions(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
        strategy: str | None,
        limit: int,
        cursor: str | None,
    ) -> RetrospectiveDecisionsResponse:
        del strategy
        del cursor  # cursor pagination is a follow-up; MVP returns top `limit`.

        stmt = (
            select(
                ResearchSummary.id.label("summary_id"),
                ResearchSession.id.label("session_id"),
                StockInfo.symbol,
                StockInfo.instrument_type,
                ResearchSummary.executed_at,
                ResearchSummary.decision,
                TradingDecisionProposal.id.label("proposal_id"),
                TradingDecisionProposal.user_response,
            )
            .join(ResearchSession, ResearchSummary.session_id == ResearchSession.id)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .join(
                TradingDecisionProposal,
                TradingDecisionProposal.symbol == StockInfo.symbol,
                isouter=True,
            )
            .where(
                ResearchSummary.executed_at >= period_start,
                ResearchSummary.executed_at < period_end,
            )
            .order_by(desc(ResearchSummary.executed_at))
            .limit(limit)
        )

        if market is not None:
            stmt = stmt.where(StockInfo.instrument_type == _MARKET_TO_INSTRUMENT[market])

        rows = (await self.db.execute(stmt)).all()
        outcome_pnls = await self._final_pnl_by_proposal(
            [int(r.proposal_id) for r in rows if r.proposal_id is not None]
        )

        result_rows: list[RetrospectiveDecisionRow] = []
        for r in rows:
            instr = str(r.instrument_type)
            market_label: Market = (
                "KR" if instr == "equity_kr"
                else "US" if instr == "equity_us"
                else "CRYPTO"
            )
            result_rows.append(
                RetrospectiveDecisionRow(
                    research_session_id=int(r.session_id),
                    symbol=str(r.symbol),
                    market=market_label,
                    decided_at=r.executed_at.isoformat(),
                    ai_decision=r.decision,
                    user_response=str(r.user_response) if r.user_response else None,
                    realized_pnl_pct=outcome_pnls.get(
                        int(r.proposal_id) if r.proposal_id else 0
                    ),
                    unrealized_pnl_pct_7d=None,  # MVP: realized only; unrealized is follow-up
                    proposal_id=int(r.proposal_id) if r.proposal_id is not None else None,
                )
            )

        return RetrospectiveDecisionsResponse(
            total=len(result_rows),
            rows=result_rows,
            next_cursor=None,
        )

    # ---------- private helpers ----------

    def _summary_query(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> Any:
        stmt = (
            select(
                ResearchSummary.id.label("summary_id"),
                ResearchSummary.session_id,
                ResearchSummary.decision,
                ResearchSummary.executed_at,
            )
            .join(ResearchSession, ResearchSummary.session_id == ResearchSession.id)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .where(
                ResearchSummary.executed_at >= period_start,
                ResearchSummary.executed_at < period_end,
            )
        )
        if market is not None:
            stmt = stmt.where(StockInfo.instrument_type == _MARKET_TO_INSTRUMENT[market])
        return stmt

    async def _proposal_response_counts(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> dict[str, int]:
        stmt = (
            select(TradingDecisionProposal.user_response, func.count())
            .join(
                TradingDecisionSession,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .where(
                TradingDecisionSession.generated_at >= period_start,
                TradingDecisionSession.generated_at < period_end,
            )
            .group_by(TradingDecisionProposal.user_response)
        )
        if market is not None:
            instrument = _MARKET_TO_INSTRUMENT[market]
            stmt = stmt.where(TradingDecisionProposal.instrument_type == instrument)
        rows = (await self.db.execute(stmt)).all()
        out: dict[str, int] = {}
        for response, count in rows:
            if response in {
                "accept",
                "reject",
                "modify",
                "partial_accept",
                "defer",
                "pending",
            }:
                key = "modify" if response == "partial_accept" else response
                out[key] = out.get(key, 0) + int(count)
        return out

    async def _stage_coverage(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> list[StageCoverageStat]:
        rows = (await self.db.execute(self._summary_query(period_start, period_end, market))).all()
        if not rows:
            return [
                StageCoverageStat(stage_type=s, coverage_pct=0.0, stale_pct=0.0, unavailable_pct=0.0)
                for s in ("market", "news", "fundamentals", "social")
            ]
        summary_ids = [row.summary_id for row in rows]
        total_sessions = len({row.session_id for row in rows})
        stage_rows = (
            await self.db.execute(
                select(
                    StageAnalysis.stage_type,
                    StageAnalysis.verdict,
                    StageAnalysis.source_freshness,
                    SummaryStageLink.summary_id,
                )
                .join(SummaryStageLink, SummaryStageLink.stage_analysis_id == StageAnalysis.id)
                .where(SummaryStageLink.summary_id.in_(summary_ids))
            )
        ).all()

        per_stage: dict[str, dict[str, int]] = {}
        for stage, verdict, freshness, _summary_id in stage_rows:
            bucket = per_stage.setdefault(
                str(stage), {"covered": 0, "stale": 0, "unavailable": 0}
            )
            bucket["covered"] += 1
            if verdict == "unavailable":
                bucket["unavailable"] += 1
            stale_flags = (
                freshness.get("stale_flags") if isinstance(freshness, dict) else None
            )
            if stale_flags:
                bucket["stale"] += 1

        out: list[StageCoverageStat] = []
        for stage in ("market", "news", "fundamentals", "social"):
            b = per_stage.get(stage, {"covered": 0, "stale": 0, "unavailable": 0})
            denom = total_sessions if total_sessions else 1
            out.append(
                StageCoverageStat(
                    stage_type=stage,  # type: ignore[arg-type]
                    coverage_pct=b["covered"] / denom * 100.0,
                    stale_pct=b["stale"] / denom * 100.0,
                    unavailable_pct=b["unavailable"] / denom * 100.0,
                )
            )
        return out

    async def _pnl_summary(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> PnlSummary:
        stmt = (
            select(TradingDecisionOutcome.pnl_pct, TradingDecisionOutcome.horizon)
            .join(
                TradingDecisionProposal,
                TradingDecisionOutcome.proposal_id == TradingDecisionProposal.id,
            )
            .join(
                TradingDecisionSession,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .where(
                TradingDecisionSession.generated_at >= period_start,
                TradingDecisionSession.generated_at < period_end,
            )
        )
        if market is not None:
            stmt = stmt.where(
                TradingDecisionProposal.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )
        rows = (await self.db.execute(stmt)).all()
        realized = [
            float(pnl) for pnl, horizon in rows if horizon == "final" and pnl is not None
        ]
        unrealized = [
            float(pnl)
            for pnl, horizon in rows
            if horizon != "final" and pnl is not None
        ]
        return PnlSummary(
            realized_pnl_pct_avg=(sum(realized) / len(realized)) if realized else None,
            unrealized_pnl_pct_avg=(sum(unrealized) / len(unrealized)) if unrealized else None,
            sample_size=len(rows),
        )

    async def _outcomes_by_proposal(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> dict[int, list[float]]:
        stmt = (
            select(
                TradingDecisionProposal.id,
                ResearchSession.id.label("session_id"),
                TradingDecisionOutcome.pnl_pct,
                TradingDecisionOutcome.horizon,
            )
            .join(
                TradingDecisionOutcome,
                TradingDecisionOutcome.proposal_id == TradingDecisionProposal.id,
            )
            .join(
                TradingDecisionSession,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .join(StockInfo, StockInfo.symbol == TradingDecisionProposal.symbol)
            .join(ResearchSession, ResearchSession.stock_info_id == StockInfo.id)
            .where(
                TradingDecisionSession.generated_at >= period_start,
                TradingDecisionSession.generated_at < period_end,
                TradingDecisionOutcome.horizon == "final",
            )
        )
        if market is not None:
            stmt = stmt.where(
                TradingDecisionProposal.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )
        rows = (await self.db.execute(stmt)).all()
        out: dict[int, list[float]] = {}
        for _proposal_id, session_id, pnl, _horizon in rows:
            if pnl is None:
                continue
            out.setdefault(int(session_id), []).append(float(pnl))
        return out

    async def _final_pnl_by_proposal(
        self, proposal_ids: list[int]
    ) -> dict[int, float]:
        if not proposal_ids:
            return {}
        stmt = (
            select(TradingDecisionOutcome.proposal_id, TradingDecisionOutcome.pnl_pct)
            .where(
                TradingDecisionOutcome.proposal_id.in_(proposal_ids),
                TradingDecisionOutcome.horizon == "final",
                TradingDecisionOutcome.pnl_pct.is_not(None),
            )
        )
        rows = (await self.db.execute(stmt)).all()
        return {int(pid): float(pnl) for pid, pnl in rows}
```

- [ ] **Step 4: Run service tests**

Run: `uv run pytest tests/services/test_research_retrospective_service.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/research_retrospective_service.py tests/services/test_research_retrospective_service.py
git commit -m "feat(ROB-121): add ResearchRetrospectiveService aggregator"
```

## B3. Backend — Router

**Files:**
- Create: `app/routers/research_retrospective.py`
- Modify: `app/main.py`
- Test: `tests/routers/test_research_retrospective_router.py`

- [ ] **Step 1: Write failing router tests**

```python
# tests/routers/test_research_retrospective_router.py
import pytest


@pytest.mark.asyncio
async def test_overview_default_window_returns_warning_when_empty(
    client_with_user,
) -> None:
    res = await client_with_user.get(
        "/trading/api/research-retrospective/overview?days=30"
    )
    assert res.status_code == 200
    body = res.json()
    assert "no_research_summaries_in_window" in body["warnings"]


@pytest.mark.asyncio
async def test_overview_market_filter_passes_through(
    client_with_user, seed_summary_buy_005930
) -> None:
    res = await client_with_user.get(
        "/trading/api/research-retrospective/overview?days=30&market=KR"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["market"] == "KR"
    assert body["sessions_total"] >= 1


@pytest.mark.asyncio
async def test_stage_performance_returns_array(client_with_user) -> None:
    res = await client_with_user.get(
        "/trading/api/research-retrospective/stage-performance?days=30"
    )
    assert res.status_code == 200
    assert isinstance(res.json(), list)


@pytest.mark.asyncio
async def test_decisions_paginates_with_limit(client_with_user) -> None:
    res = await client_with_user.get(
        "/trading/api/research-retrospective/decisions?days=30&limit=5"
    )
    assert res.status_code == 200
    body = res.json()
    assert "rows" in body
    assert len(body["rows"]) <= 5
```

- [ ] **Step 2: Confirm fail**

Run: `uv run pytest tests/routers/test_research_retrospective_router.py -v`

- [ ] **Step 3: Implement the router**

```python
# app/routers/research_retrospective.py
"""ROB-121 — Research retrospective router (read-only)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.research_retrospective import (
    RetrospectiveDecisionsResponse,
    RetrospectiveOverview,
    StagePerformanceRow,
)
from app.services.research_retrospective_service import (
    ResearchRetrospectiveService,
)

api_router = APIRouter(
    prefix="/api/research-retrospective", tags=["research-retrospective"]
)
router = APIRouter()


def _service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ResearchRetrospectiveService:
    return ResearchRetrospectiveService(db)


def _resolve_window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return start, end


@api_router.get("/overview", response_model=RetrospectiveOverview)
async def get_overview(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    svc: Annotated[ResearchRetrospectiveService, Depends(_service)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Annotated[
        Literal["KR", "US", "CRYPTO"] | None, Query()
    ] = None,
    strategy: Annotated[str | None, Query(max_length=100)] = None,
) -> RetrospectiveOverview:
    start, end = _resolve_window(days)
    return await svc.build_overview(
        period_start=start, period_end=end, market=market, strategy=strategy
    )


@api_router.get(
    "/stage-performance",
    response_model=list[StagePerformanceRow],
)
async def get_stage_performance(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    svc: Annotated[ResearchRetrospectiveService, Depends(_service)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Annotated[
        Literal["KR", "US", "CRYPTO"] | None, Query()
    ] = None,
    strategy: Annotated[str | None, Query(max_length=100)] = None,
) -> list[StagePerformanceRow]:
    start, end = _resolve_window(days)
    return await svc.build_stage_performance(
        period_start=start, period_end=end, market=market, strategy=strategy
    )


@api_router.get(
    "/decisions",
    response_model=RetrospectiveDecisionsResponse,
)
async def list_decisions(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    svc: Annotated[ResearchRetrospectiveService, Depends(_service)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Annotated[
        Literal["KR", "US", "CRYPTO"] | None, Query()
    ] = None,
    strategy: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
) -> RetrospectiveDecisionsResponse:
    start, end = _resolve_window(days)
    return await svc.list_decisions(
        period_start=start,
        period_end=end,
        market=market,
        strategy=strategy,
        limit=limit,
        cursor=cursor,
    )


router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
```

- [ ] **Step 4: Register router**

In `app/main.py`, near `portfolio_actions.router`:

```python
from app.routers import research_retrospective
# ...
app.include_router(research_retrospective.router)
```

- [ ] **Step 5: Run router tests**

Run: `uv run pytest tests/routers/test_research_retrospective_router.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add app/routers/research_retrospective.py app/main.py tests/routers/test_research_retrospective_router.py
git commit -m "feat(ROB-121): add /trading/api/research-retrospective router"
```

## B4. Frontend — types, API, route, i18n

**Files:**
- Modify: `frontend/trading-decision/src/api/types.ts`
- Create: `frontend/trading-decision/src/api/researchRetrospective.ts`
- Modify: `frontend/trading-decision/src/routes.tsx`
- Modify: `frontend/trading-decision/src/i18n/ko.ts`

- [ ] **Step 1: Add types**

Append to `frontend/trading-decision/src/api/types.ts`:

```ts
export type DecisionVerdict = "buy" | "hold" | "sell";
export type StageType = "market" | "news" | "fundamentals" | "social";

export interface StageCoverageStat {
  stage_type: StageType;
  coverage_pct: number;
  stale_pct: number;
  unavailable_pct: number;
}

export interface DecisionDistribution {
  ai_buy: number;
  ai_hold: number;
  ai_sell: number;
  user_accept: number;
  user_reject: number;
  user_modify: number;
  user_defer: number;
  user_pending: number;
}

export interface PnlSummary {
  realized_pnl_pct_avg: number | null;
  unrealized_pnl_pct_avg: number | null;
  sample_size: number;
}

export interface RetrospectiveOverview {
  period_start: string;
  period_end: string;
  market: Market | null;
  strategy: string | null;
  sessions_total: number;
  summaries_total: number;
  decision_distribution: DecisionDistribution;
  stage_coverage: StageCoverageStat[];
  pnl: PnlSummary;
  warnings: string[];
}

export interface StagePerformanceRow {
  stage_combo: string;
  sample_size: number;
  win_rate_pct: number | null;
  avg_realized_pnl_pct: number | null;
}

export interface RetrospectiveDecisionRow {
  research_session_id: number;
  symbol: string;
  market: Market;
  decided_at: string;
  ai_decision: DecisionVerdict | null;
  user_response: string | null;
  realized_pnl_pct: number | null;
  unrealized_pnl_pct_7d: number | null;
  proposal_id: number | null;
}

export interface RetrospectiveDecisionsResponse {
  total: number;
  rows: RetrospectiveDecisionRow[];
  next_cursor: string | null;
}
```

- [ ] **Step 2: Create the API wrapper**

```ts
// frontend/trading-decision/src/api/researchRetrospective.ts
import { apiFetch } from "./client";
import type {
  Market,
  RetrospectiveDecisionsResponse,
  RetrospectiveOverview,
  StagePerformanceRow,
} from "./types";

interface CommonFilters {
  days?: number;
  market?: Market;
  strategy?: string;
}

function qs(filters: CommonFilters & Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(filters)) {
    if (v === undefined || v === null) continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export function getRetrospectiveOverview(
  filters: CommonFilters,
): Promise<RetrospectiveOverview> {
  return apiFetch<RetrospectiveOverview>(
    `/research-retrospective/overview${qs(filters)}`,
  );
}

export function getRetrospectiveStagePerformance(
  filters: CommonFilters,
): Promise<StagePerformanceRow[]> {
  return apiFetch<StagePerformanceRow[]>(
    `/research-retrospective/stage-performance${qs(filters)}`,
  );
}

export function listRetrospectiveDecisions(
  filters: CommonFilters & { limit?: number; cursor?: string },
): Promise<RetrospectiveDecisionsResponse> {
  return apiFetch<RetrospectiveDecisionsResponse>(
    `/research-retrospective/decisions${qs(filters)}`,
  );
}
```

- [ ] **Step 3: Add the route**

In `frontend/trading-decision/src/routes.tsx`:

```tsx
import RetrospectivePage from "./pages/RetrospectivePage";
// ...
{ path: "/retrospective", element: <RetrospectivePage /> },
```

- [ ] **Step 4: Add i18n strings**

Append to `i18n/ko.ts`:

```ts
export const retrospective = {
  pageTitle: "Research Retrospective",
  pageSubtitle: "Research Pipeline 분석/요약/결정/주문 결과 회고",
  filterDays: "기간(일)",
  filterMarket: "시장",
  filterAll: "전체",
  marketKR: "KR",
  marketUS: "US",
  marketCRYPTO: "암호화폐",
  loadError: "Retrospective 로딩 실패",
  empty: "선택한 조건에 해당하는 데이터가 없습니다.",
  warningEmpty: "기간 내 Research Summary가 없습니다.",
  cards: {
    sessions: "Sessions",
    summaries: "Summaries",
    realizedPnl: "실현 PnL 평균",
    unrealizedPnl: "미실현 PnL 평균",
  },
  distribution: {
    title: "AI vs User Decision",
    aiBuy: "AI buy",
    aiHold: "AI hold",
    aiSell: "AI sell",
    userAccept: "User accept",
    userReject: "User reject",
    userModify: "User modify",
    userDefer: "User defer",
    userPending: "User pending",
  },
  stageCoverage: {
    title: "Stage Coverage",
    stage: "Stage",
    coverage: "Coverage %",
    stale: "Stale %",
    unavailable: "Unavailable %",
  },
  stagePerformance: {
    title: "Stage 조합별 성과",
    combo: "조합",
    sample: "표본",
    winRate: "승률 %",
    avgPnl: "평균 PnL %",
  },
  decisions: {
    title: "Decision drill-down",
    symbol: "종목",
    market: "시장",
    decidedAt: "결정 시각",
    ai: "AI",
    user: "사용자",
    realized: "실현 PnL %",
    open: "Session 열기",
  },
};
```

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/api/types.ts frontend/trading-decision/src/api/researchRetrospective.ts frontend/trading-decision/src/routes.tsx frontend/trading-decision/src/i18n/ko.ts
git commit -m "feat(ROB-121): add retrospective API client, route, and i18n"
```

## B5. Frontend — `RetrospectiveStagePerformance` and `RetrospectiveDecisionTable`

**Files:**
- Create: `frontend/trading-decision/src/components/RetrospectiveStagePerformance.tsx`
- Create: `frontend/trading-decision/src/components/RetrospectiveDecisionTable.tsx`

- [ ] **Step 1: Implement the components**

```tsx
// frontend/trading-decision/src/components/RetrospectiveStagePerformance.tsx
import type { StagePerformanceRow } from "../api/types";
import { retrospective as t } from "../i18n/ko";

function fmtPct(v: number | null): string {
  return v === null ? "-" : `${v.toFixed(1)}%`;
}

interface Props {
  rows: StagePerformanceRow[];
}

export default function RetrospectiveStagePerformance({ rows }: Props) {
  if (rows.length === 0) return <p>{t.empty}</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>{t.stagePerformance.combo}</th>
          <th>{t.stagePerformance.sample}</th>
          <th>{t.stagePerformance.winRate}</th>
          <th>{t.stagePerformance.avgPnl}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.stage_combo}>
            <td>{r.stage_combo}</td>
            <td>{r.sample_size}</td>
            <td>{fmtPct(r.win_rate_pct)}</td>
            <td>{fmtPct(r.avg_realized_pnl_pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

```tsx
// frontend/trading-decision/src/components/RetrospectiveDecisionTable.tsx
import type { RetrospectiveDecisionRow } from "../api/types";
import { retrospective as t } from "../i18n/ko";

function fmtPct(v: number | null): string {
  return v === null ? "-" : `${v.toFixed(2)}%`;
}

interface Props {
  rows: RetrospectiveDecisionRow[];
}

export default function RetrospectiveDecisionTable({ rows }: Props) {
  if (rows.length === 0) return <p>{t.empty}</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>{t.decisions.symbol}</th>
          <th>{t.decisions.market}</th>
          <th>{t.decisions.decidedAt}</th>
          <th>{t.decisions.ai}</th>
          <th>{t.decisions.user}</th>
          <th>{t.decisions.realized}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.research_session_id}-${r.proposal_id ?? "noprop"}`}>
            <td>{r.symbol}</td>
            <td>{r.market}</td>
            <td>{r.decided_at}</td>
            <td>{r.ai_decision ?? "-"}</td>
            <td>{r.user_response ?? "-"}</td>
            <td>{fmtPct(r.realized_pnl_pct)}</td>
            <td>
              <a
                href={`/trading/decisions/research/sessions/${r.research_session_id}/summary`}
              >
                {t.decisions.open}
              </a>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/trading-decision/src/components/RetrospectiveStagePerformance.tsx frontend/trading-decision/src/components/RetrospectiveDecisionTable.tsx
git commit -m "feat(ROB-121): add retrospective table components"
```

## B6. Frontend — `RetrospectivePage`

**Files:**
- Create: `frontend/trading-decision/src/pages/RetrospectivePage.tsx`
- Create: `frontend/trading-decision/src/pages/RetrospectivePage.module.css`
- Create: `frontend/trading-decision/src/__tests__/RetrospectivePage.test.tsx`

- [ ] **Step 1: Write the failing page test**

```tsx
// frontend/trading-decision/src/__tests__/RetrospectivePage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api/researchRetrospective", () => ({
  getRetrospectiveOverview: vi.fn(),
  getRetrospectiveStagePerformance: vi.fn(),
  listRetrospectiveDecisions: vi.fn(),
}));

import * as api from "../api/researchRetrospective";
import RetrospectivePage from "../pages/RetrospectivePage";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

beforeEach(() => {
  (api.getRetrospectiveOverview as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    period_start: "2026-04-06T00:00:00Z",
    period_end: "2026-05-06T00:00:00Z",
    market: null,
    strategy: null,
    sessions_total: 5,
    summaries_total: 7,
    decision_distribution: {
      ai_buy: 2, ai_hold: 3, ai_sell: 2,
      user_accept: 1, user_reject: 1, user_modify: 0, user_defer: 0, user_pending: 5,
    },
    stage_coverage: [
      { stage_type: "market", coverage_pct: 100, stale_pct: 0, unavailable_pct: 0 },
      { stage_type: "news", coverage_pct: 60, stale_pct: 30, unavailable_pct: 10 },
      { stage_type: "fundamentals", coverage_pct: 30, stale_pct: 0, unavailable_pct: 70 },
      { stage_type: "social", coverage_pct: 0, stale_pct: 0, unavailable_pct: 100 },
    ],
    pnl: { realized_pnl_pct_avg: 1.2, unrealized_pnl_pct_avg: -0.5, sample_size: 6 },
    warnings: [],
  });
  (api.getRetrospectiveStagePerformance as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([
    { stage_combo: "market+news", sample_size: 4, win_rate_pct: 75, avg_realized_pnl_pct: 2.1 },
  ]);
  (api.listRetrospectiveDecisions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    total: 1,
    rows: [
      {
        research_session_id: 11,
        symbol: "005930",
        market: "KR",
        decided_at: "2026-05-01T00:00:00Z",
        ai_decision: "buy",
        user_response: "accept",
        realized_pnl_pct: 3.4,
        unrealized_pnl_pct_7d: null,
        proposal_id: 22,
      },
    ],
    next_cursor: null,
  });
});

describe("RetrospectivePage", () => {
  it("renders overview cards and decision drill-down", async () => {
    render(<RetrospectivePage />);
    await waitFor(() => {
      expect(screen.getByText(/Research Retrospective/)).toBeInTheDocument();
    });
    expect(await screen.findByText("005930")).toBeInTheDocument();
    expect(screen.getByText(/3.40%/)).toBeInTheDocument();
  });

  it("renders empty warning when sessions_total is 0", async () => {
    (api.getRetrospectiveOverview as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      period_start: "2026-04-06T00:00:00Z",
      period_end: "2026-05-06T00:00:00Z",
      market: null,
      strategy: null,
      sessions_total: 0,
      summaries_total: 0,
      decision_distribution: {
        ai_buy: 0, ai_hold: 0, ai_sell: 0,
        user_accept: 0, user_reject: 0, user_modify: 0, user_defer: 0, user_pending: 0,
      },
      stage_coverage: [],
      pnl: { realized_pnl_pct_avg: null, unrealized_pnl_pct_avg: null, sample_size: 0 },
      warnings: ["no_research_summaries_in_window"],
    });
    render(<RetrospectivePage />);
    expect(await screen.findByText(/Research Summary가 없습니다/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Confirm fail**

Run: `cd frontend/trading-decision && npm test -- RetrospectivePage`

- [ ] **Step 3: Implement the page**

```tsx
// frontend/trading-decision/src/pages/RetrospectivePage.tsx
import { useEffect, useState } from "react";

import RetrospectiveDecisionTable from "../components/RetrospectiveDecisionTable";
import RetrospectiveStagePerformance from "../components/RetrospectiveStagePerformance";
import {
  getRetrospectiveOverview,
  getRetrospectiveStagePerformance,
  listRetrospectiveDecisions,
} from "../api/researchRetrospective";
import type {
  Market,
  RetrospectiveDecisionsResponse,
  RetrospectiveOverview,
  StagePerformanceRow,
} from "../api/types";
import { retrospective as t } from "../i18n/ko";
import styles from "./RetrospectivePage.module.css";

const DAYS_OPTIONS = [7, 14, 30, 60, 90];

function fmtPct(v: number | null): string {
  return v === null ? "-" : `${v.toFixed(2)}%`;
}

export default function RetrospectivePage() {
  const [days, setDays] = useState(30);
  const [market, setMarket] = useState<Market | "ALL">("ALL");
  const [overview, setOverview] = useState<RetrospectiveOverview | null>(null);
  const [stage, setStage] = useState<StagePerformanceRow[]>([]);
  const [decisions, setDecisions] =
    useState<RetrospectiveDecisionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    const filters = {
      days,
      market: market === "ALL" ? undefined : (market as Market),
    };
    Promise.all([
      getRetrospectiveOverview(filters),
      getRetrospectiveStagePerformance(filters),
      listRetrospectiveDecisions({ ...filters, limit: 20 }),
    ])
      .then(([ov, stg, dec]) => {
        if (cancelled) return;
        setOverview(ov);
        setStage(stg);
        setDecisions(dec);
      })
      .catch(() => {
        if (!cancelled) setError(t.loadError);
      });
    return () => {
      cancelled = true;
    };
  }, [days, market]);

  const empty =
    overview !== null &&
    overview.warnings.includes("no_research_summaries_in_window");

  return (
    <main className={styles.page}>
      <header>
        <h1>{t.pageTitle}</h1>
        <p>{t.pageSubtitle}</p>
      </header>

      <div className={styles.filters}>
        <label>
          {t.filterDays}
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {DAYS_OPTIONS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t.filterMarket}
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as Market | "ALL")}
          >
            <option value="ALL">{t.filterAll}</option>
            <option value="KR">{t.marketKR}</option>
            <option value="US">{t.marketUS}</option>
            <option value="CRYPTO">{t.marketCRYPTO}</option>
          </select>
        </label>
      </div>

      {error ? <p className={styles.error}>{error}</p> : null}
      {empty ? <p className={styles.warning}>{t.warningEmpty}</p> : null}

      {overview ? (
        <>
          <section className={styles.cards}>
            <div className={styles.card}>
              <h3>{t.cards.sessions}</h3>
              <p>{overview.sessions_total}</p>
            </div>
            <div className={styles.card}>
              <h3>{t.cards.summaries}</h3>
              <p>{overview.summaries_total}</p>
            </div>
            <div className={styles.card}>
              <h3>{t.cards.realizedPnl}</h3>
              <p>{fmtPct(overview.pnl.realized_pnl_pct_avg)}</p>
            </div>
            <div className={styles.card}>
              <h3>{t.cards.unrealizedPnl}</h3>
              <p>{fmtPct(overview.pnl.unrealized_pnl_pct_avg)}</p>
            </div>
          </section>

          <section>
            <h2>{t.distribution.title}</h2>
            <ul className={styles.kv}>
              <li>{t.distribution.aiBuy}: {overview.decision_distribution.ai_buy}</li>
              <li>{t.distribution.aiHold}: {overview.decision_distribution.ai_hold}</li>
              <li>{t.distribution.aiSell}: {overview.decision_distribution.ai_sell}</li>
              <li>{t.distribution.userAccept}: {overview.decision_distribution.user_accept}</li>
              <li>{t.distribution.userReject}: {overview.decision_distribution.user_reject}</li>
              <li>{t.distribution.userModify}: {overview.decision_distribution.user_modify}</li>
              <li>{t.distribution.userDefer}: {overview.decision_distribution.user_defer}</li>
              <li>{t.distribution.userPending}: {overview.decision_distribution.user_pending}</li>
            </ul>
          </section>

          <section>
            <h2>{t.stageCoverage.title}</h2>
            <table>
              <thead>
                <tr>
                  <th>{t.stageCoverage.stage}</th>
                  <th>{t.stageCoverage.coverage}</th>
                  <th>{t.stageCoverage.stale}</th>
                  <th>{t.stageCoverage.unavailable}</th>
                </tr>
              </thead>
              <tbody>
                {overview.stage_coverage.map((s) => (
                  <tr key={s.stage_type}>
                    <td>{s.stage_type}</td>
                    <td>{s.coverage_pct.toFixed(1)}%</td>
                    <td>{s.stale_pct.toFixed(1)}%</td>
                    <td>{s.unavailable_pct.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      ) : null}

      <section>
        <h2>{t.stagePerformance.title}</h2>
        <RetrospectiveStagePerformance rows={stage} />
      </section>

      <section>
        <h2>{t.decisions.title}</h2>
        <RetrospectiveDecisionTable rows={decisions?.rows ?? []} />
      </section>
    </main>
  );
}
```

```css
/* frontend/trading-decision/src/pages/RetrospectivePage.module.css */
.page { padding: 24px; display: flex; flex-direction: column; gap: 24px; }
.filters { display: flex; gap: 16px; }
.filters label { display: flex; flex-direction: column; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.card {
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
  padding: 16px;
}
.card h3 { margin: 0 0 8px; font-size: 13px; color: #475569; }
.card p { margin: 0; font-size: 22px; font-weight: 600; }
.kv { list-style: none; padding: 0; display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.error { color: #b91c1c; }
.warning {
  background: #fef3c7; border: 1px solid #fde68a; padding: 12px;
  border-radius: 4px;
}
```

- [ ] **Step 4: Run page test**

Run: `cd frontend/trading-decision && npm test -- RetrospectivePage`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/pages/RetrospectivePage.tsx frontend/trading-decision/src/pages/RetrospectivePage.module.css frontend/trading-decision/src/__tests__/RetrospectivePage.test.tsx
git commit -m "feat(ROB-121): add RetrospectivePage"
```

## B7. End-to-end checks for ROB-121

- [ ] **Step 1: Backend type/lint/tests**

```bash
make lint
uv run pytest tests/services/test_research_retrospective_service.py tests/routers/test_research_retrospective_router.py -v
```
Expected: all green.

- [ ] **Step 2: Frontend typecheck/test/build**

```bash
cd frontend/trading-decision
npm run typecheck
npm test
npm run build
```
Expected: typecheck clean, all retrospective tests pass, build succeeds.

- [ ] **Step 3: Smoke test locally**

Run `make dev`, visit `http://localhost:8000/trading/decisions/retrospective`. Verify:
- empty period-window scenarios show the warning instead of blank cards
- changing `days` and `market` re-fires the fetch and updates all three sections
- "Session 열기" link navigates to `/trading/decisions/research/sessions/<id>/summary`
- no broker/order calls fired (check `tail -f logs/auto_trader.log` while paging through filters)

- [ ] **Step 4: ROB-121 PR**

```bash
git push -u origin feature/ROB-121-research-retrospective
gh pr create --base main --title "feat(ROB-121): Research Retrospective page" --body "Closes ROB-121. ..."
```

---

## Self-review checklist

Before opening either PR, walk through this list and fix anything in place.

- [ ] **No DB migrations.** `git status` shows zero files under `alembic/versions/`. The whole point of ROB-120's metadata-on-JSONB design is that we ship without one.
- [ ] **No broker/order/scheduler imports.** `grep -E "kis_trading|alpaca_paper|paper_trading|broker_account|taskiq|scheduler"` against the new files in this PR returns nothing.
- [ ] **No direct DB writes outside `TradeJournalWriteService`.** ROB-121 service code never calls `db.add`, `db.delete`, or `INSERT/UPDATE/DELETE`. Verify with `grep -E "db\\.add|db\\.delete|INSERT|UPDATE |DELETE" app/services/research_retrospective_service.py app/routers/research_retrospective.py`.
- [ ] **Status guard.** `_apply_update` rejects every value of `JournalStatus` outside `{draft, active}`, with a unit test covering each. ROB-120 router test for `status: closed` returns 422.
- [ ] **Conflict detection symmetry.** `thesis_conflict_with_summary` is `True` only when the journal is `active` AND latest summary decision is `sell`. Check the unit test asserts both sides.
- [ ] **Korean i18n.** Every visible string in the new pages comes through `i18n/ko`. No hard-coded English strings outside data fields (`stage_combo`, etc.).
- [ ] **Empty states.** Journal page renders `{t.empty}` for zero holdings; retrospective page renders `{t.warningEmpty}` when `no_research_summaries_in_window` warning is present. Both have an explicit test.
- [ ] **Symbol-to-DB normalization.** Anywhere a US ticker enters from the UI (rare in this PR, but possible for the journal create flow), it is normalized via `app.core.symbol.to_db_symbol` before SQL — confirmed in `TradeJournalCoverageService` and the create handler.
- [ ] **Spec coverage check.** Each AC item from ROB-120 and ROB-121 maps to a step in this plan. Walk both AC lists and check that every bullet (e.g. "frontend typecheck/test/build 통과", "Portfolio Action Board가 journal_status를 참조할 수 있다") is satisfied. The Portfolio Action Board already reads `journal_status` from the existing `PortfolioDashboardService` snapshot (per `app/services/portfolio_action_service.py:_load_journal_status`); ROB-120 keeps the contract intact and adds no new dependencies.
- [ ] **Plan placeholders.** `grep -nE "TODO|TBD|fill in|appropriate error handling" docs/plans/ROB-120-121-thesis-journal-and-retrospective-plan.md` returns nothing.

---

## Execution Handoff

Plan complete and saved to `docs/plans/ROB-120-121-thesis-journal-and-retrospective-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh Sonnet implementer per task in this plan, you review between tasks, fast iteration. Best for ROB-120 first, then ROB-121.
2. **Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batching with checkpoints between Part A and Part B.

Which approach?
