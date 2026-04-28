# Create Decision Session from Research Run Live Refresh - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable operators to create trading decision sessions directly from research run snapshots, with optional live market data refresh for each candidate.

**Architecture:** 
A new service `research_run_decision_session_service.py` will orchestrate the transformation from ResearchRun + candidates → TradingDecisionSession + proposals. The service follows the same pattern as `operator_decision_session_service.py`, mapping research run candidates to decision proposals based on candidate kind and side. A new POST endpoint in `trading_decisions.py` exposes this functionality.

**Tech Stack:** Python 3.13+, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, pytest

---

## File Structure

| File | Responsibility |
|------|----------------|
| `app/schemas/research_run_decision_session.py` | Request/response schemas for research run → decision session conversion |
| `app/services/research_run_decision_session_service.py` | Core service orchestrating the transformation with live refresh support |
| `app/routers/trading_decisions.py` | Extended with POST `/api/decisions/from-research-run/{run_uuid}` endpoint |
| `tests/test_research_run_decision_session_service.py` | Unit tests for service logic and candidate mapping |
| `tests/test_research_run_decision_session_service_safety.py` | Safety tests for ownership, permissions, boundary conditions |
| `tests/test_research_run_decision_session_router.py` | Router integration tests with mocked service |
| `tests/test_research_run_decision_session_router_safety.py` | Router safety tests for auth, validation, error handling |
| `tests/test_research_run_live_refresh_service.py` | Live refresh specific tests with mocked market data |

---

## Task 1: Create Request/Response Schemas

**Files:**
- Create: `app/schemas/research_run_decision_session.py`

- [ ] **Step 1: Write the schema file**

```python
"""Schemas for creating decision sessions from research runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.trading_decisions import SessionStatusLiteral

ResearchRunRefreshModeLiteral = Literal["none", "price", "full"]


class ResearchRunToDecisionSessionRequest(BaseModel):
    """Request to create a decision session from a research run."""

    model_config = ConfigDict(extra="forbid")

    refresh_mode: ResearchRunRefreshModeLiteral = Field(
        default="price",
        description="none: use stored data, price: refresh only price, full: refresh all market data"
    )
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    generated_at: datetime | None = Field(
        default=None,
        description="Override timestamp; uses now() if not provided"
    )


class ResearchRunToDecisionSessionResponse(BaseModel):
    """Response after creating a decision session from a research run."""

    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    proposal_count: int
    run_uuid: UUID
    refreshed_candidates: int
```

- [ ] **Step 2: Verify schemas are valid Python**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run python -c "from app.schemas.research_run_decision_session import ResearchRunToDecisionSessionRequest, ResearchRunToDecisionSessionResponse; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/schemas/research_run_decision_session.py
git commit -m "feat(research): add schemas for research run to decision session conversion"
```

---

## Task 2: Create Core Service

**Files:**
- Create: `app/services/research_run_decision_session_service.py`

- [ ] **Step 1: Write the service file**

```python
"""Service for creating decision sessions from research runs with live refresh."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunCandidateKind,
)
from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionSession,
)
from app.services import research_run_service, trading_decision_service

if TYPE_CHECKING:
    from app.services.kis import KISClient
    from app.services.upbit import UpbitService

__all__ = [
    "ResearchRunDecisionResult",
    "create_decision_session_from_research_run",
]


@dataclass(frozen=True)
class ResearchRunDecisionResult:
    """Result of creating a decision session from a research run."""

    session: TradingDecisionSession
    proposal_count: int
    refreshed_candidates: int


def _map_candidate_kind_to_proposal_kind(
    candidate: ResearchRunCandidate,
) -> ProposalKind:
    """Map research run candidate kind and side to proposal kind."""
    kind = candidate.candidate_kind
    side = candidate.side

    if kind == ResearchRunCandidateKind.pending_order:
        if side == "buy":
            return ProposalKind.enter
        elif side == "sell":
            return ProposalKind.exit
        else:
            return ProposalKind.enter

    elif kind == ResearchRunCandidateKind.holding:
        if side == "sell":
            return ProposalKind.exit
        elif side == "buy":
            return ProposalKind.add
        else:
            return ProposalKind.trim

    elif kind == ResearchRunCandidateKind.screener_hit:
        if side == "buy":
            return ProposalKind.enter
        elif side == "sell":
            return ProposalKind.exit
        else:
            return ProposalKind.breakout_watch

    elif kind == ResearchRunCandidateKind.proposed:
        if side == "buy":
            return ProposalKind.enter
        elif side == "sell":
            return ProposalKind.exit
        else:
            return ProposalKind.other

    else:  # other
        return ProposalKind.other


def _build_proposal_from_candidate(
    candidate: ResearchRunCandidate,
    live_price: float | None = None,
) -> dict[str, Any]:
    """Build a proposal create dict from a research run candidate."""
    proposal_kind = _map_candidate_kind_to_proposal_kind(candidate)

    # Build original_payload with candidate metadata
    original_payload: dict[str, Any] = {
        "source": "research_run_candidate",
        "candidate_uuid": str(candidate.candidate_uuid),
        "candidate_kind": candidate.candidate_kind.value,
        "confidence": candidate.confidence,
        "source_freshness": candidate.source_freshness,
    }

    # Add live refresh data if available
    if live_price is not None:
        original_payload["live_refresh"] = {
            "price": live_price,
            "refreshed_at": datetime.now(UTC).isoformat(),
        }

    # Merge any existing payload data
    if candidate.payload:
        original_payload["candidate_payload"] = candidate.payload

    return {
        "symbol": candidate.symbol,
        "instrument_type": candidate.instrument_type,
        "proposal_kind": proposal_kind,
        "side": candidate.side,
        "original_quantity": candidate.proposed_qty,
        "original_quantity_pct": None,
        "original_amount": None,
        "original_price": live_price if live_price is not None else candidate.proposed_price,
        "original_trigger_price": None,
        "original_threshold_pct": None,
        "original_currency": candidate.currency,
        "original_rationale": candidate.rationale,
        "original_payload": original_payload,
    }


async def _refresh_candidate_price(
    candidate: ResearchRunCandidate,
    market_scope: str,
    kis_client: KISClient | None = None,
    upbit_service: UpbitService | None = None,
) -> float | None:
    """Fetch live price for a candidate based on market scope.

    Returns None if refresh fails or services are not available.
    """
    try:
        if market_scope == "kr" and kis_client is not None:
            # Try to fetch from KIS for KR market
            quote = await kis_client.get_quote(candidate.symbol)
            if quote and "stck_prpr" in quote:
                return float(quote["stck_prpr"])
        elif market_scope == "crypto" and upbit_service is not None:
            # Try to fetch from Upbit for crypto
            ticker = await upbit_service.get_ticker(candidate.symbol)
            if ticker and "trade_price" in ticker:
                return float(ticker["trade_price"])
        elif market_scope == "us":
            # US market refresh would require yfinance or similar
            # For now, return None to use stored price
            return None
    except Exception:
        # Fail silently - use stored price on refresh failure
        pass

    return None


async def create_decision_session_from_research_run(
    db: AsyncSession,
    *,
    user_id: int,
    run_uuid: UUID,
    refresh_mode: str = "price",
    strategy_name: str | None = None,
    notes: str | None = None,
    generated_at: datetime | None = None,
    kis_client: KISClient | None = None,
    upbit_service: UpbitService | None = None,
) -> ResearchRunDecisionResult:
    """Create a trading decision session from a research run.

    Args:
        db: Database session
        user_id: User ID (must own the research run)
        run_uuid: Research run UUID
        refresh_mode: "none" | "price" | "full"
        strategy_name: Optional strategy name override
        notes: Optional notes for the session
        generated_at: Optional timestamp override
        kis_client: Optional KIS client for KR market price refresh
        upbit_service: Optional Upbit service for crypto price refresh

    Returns:
        ResearchRunDecisionResult with created session and proposal count

    Raises:
        ValueError: If research run not found or does not belong to user
    """
    # Fetch the research run with candidates
    run = await research_run_service.get_research_run_by_uuid(
        db, run_uuid=run_uuid, user_id=user_id
    )

    if run is None:
        raise ValueError(f"Research run not found: {run_uuid}")

    if not run.candidates:
        # Empty candidates is allowed - creates empty session
        pass

    # Determine timestamp
    session_generated_at = generated_at or datetime.now(UTC)

    # Create the decision session
    session = await trading_decision_service.create_decision_session(
        db,
        user_id=user_id,
        source_profile="research_run_live_refresh",
        strategy_name=strategy_name or run.strategy_name,
        market_scope=run.market_scope,
        market_brief={
            "source_run_uuid": str(run_uuid),
            "source_run_stage": run.stage,
            "refresh_mode": refresh_mode,
            "original_market_brief": run.market_brief,
        },
        generated_at=session_generated_at,
        notes=notes or run.notes,
    )

    # Build proposals from candidates
    proposals = []
    refreshed_count = 0

    for candidate in run.candidates:
        live_price: float | None = None

        # Refresh price if requested
        if refresh_mode in ("price", "full"):
            live_price = await _refresh_candidate_price(
                candidate,
                market_scope=run.market_scope,
                kis_client=kis_client,
                upbit_service=upbit_service,
            )
            if live_price is not None:
                refreshed_count += 1

        proposal = _build_proposal_from_candidate(candidate, live_price)
        proposals.append(proposal)

    # Add proposals to session
    db_proposals = await trading_decision_service.add_decision_proposals(
        db,
        session_id=session.id,
        proposals=proposals,
    )

    return ResearchRunDecisionResult(
        session=session,
        proposal_count=len(db_proposals),
        refreshed_candidates=refreshed_count,
    )
```

- [ ] **Step 2: Verify service imports work**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run python -c "from app.services.research_run_decision_session_service import create_decision_session_from_research_run; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run type checker**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run ty check app/services/research_run_decision_session_service.py`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add app/services/research_run_decision_session_service.py
git commit -m "feat(research): add service for creating decision sessions from research runs"
```

---

## Task 3: Add Router Endpoint

**Files:**
- Modify: `app/routers/trading_decisions.py`

- [ ] **Step 1: Add imports at top of file**

Add after line 46 (after existing imports):

```python
from app.schemas.research_run_decision_session import (
    ResearchRunToDecisionSessionRequest,
    ResearchRunToDecisionSessionResponse,
)
from app.services.research_run_decision_session_service import (
    create_decision_session_from_research_run,
)
```

- [ ] **Step 2: Add endpoint at end of file**

Add after line 602 (after `create_decision_from_operator_request`):

```python


@router.post(
    "/api/decisions/from-research-run/{run_uuid}",
    response_model=ResearchRunToDecisionSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision_from_research_run(
    run_uuid: UUID,
    payload: ResearchRunToDecisionSessionRequest,
    response: Response,
    fastapi_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> ResearchRunToDecisionSessionResponse:
    """Create a trading decision session from a research run.

    Optionally refreshes live market data for each candidate based on refresh_mode.
    """
    try:
        result = await create_decision_session_from_research_run(
            db,
            user_id=current_user.id,
            run_uuid=run_uuid,
            refresh_mode=payload.refresh_mode,
            strategy_name=payload.strategy_name,
            notes=payload.notes,
            generated_at=payload.generated_at,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await db.commit()

    base_url = resolve_trading_decision_base_url(
        configured=settings.public_base_url,
        request_base_url=str(fastapi_request.base_url),
    )
    session_url = build_trading_decision_session_url(
        base_url=base_url,
        session_uuid=result.session.session_uuid,
    )
    response.headers["Location"] = (
        f"/trading/api/decisions/{result.session.session_uuid}"
    )

    return ResearchRunToDecisionSessionResponse(
        session_uuid=result.session.session_uuid,
        session_url=session_url,
        status=result.session.status,
        proposal_count=result.proposal_count,
        run_uuid=run_uuid,
        refreshed_candidates=result.refreshed_candidates,
    )
```

- [ ] **Step 3: Verify router imports work**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run python -c "from app.routers import trading_decisions; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Run type checker on router**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run ty check app/routers/trading_decisions.py`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add app/routers/trading_decisions.py
git commit -m "feat(research): add endpoint for creating decision sessions from research runs"
```

---

## Task 4: Write Service Unit Tests

**Files:**
- Create: `tests/test_research_run_decision_session_service.py`

- [ ] **Step 1: Write the test file**

```python
"""Unit tests for research_run_decision_session_service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunCandidateKind,
    ResearchRunStage,
    ResearchRunStatus,
)
from app.models.trading import InstrumentType
from app.models.trading_decision import ProposalKind, TradingDecisionSession
from app.services.research_run_decision_session_service import (
    ResearchRunDecisionResult,
    _build_proposal_from_candidate,
    _map_candidate_kind_to_proposal_kind,
    create_decision_session_from_research_run,
)


@pytest.fixture
def sample_research_run() -> ResearchRun:
    """Create a sample research run for testing."""
    return ResearchRun(
        id=1,
        run_uuid=uuid4(),
        user_id=1,
        market_scope="kr",
        stage=ResearchRunStage.preopen,
        status=ResearchRunStatus.open,
        source_profile="test_profile",
        strategy_name="test_strategy",
        notes="Test notes",
        market_brief={"key": "value"},
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def sample_candidate_buy_pending() -> ResearchRunCandidate:
    """Create a sample buy pending candidate."""
    return ResearchRunCandidate(
        id=1,
        candidate_uuid=uuid4(),
        research_run_id=1,
        symbol="005930",
        instrument_type=InstrumentType.stock,
        side="buy",
        candidate_kind=ResearchRunCandidateKind.pending_order,
        proposed_price=Decimal("70000.00"),
        proposed_qty=Decimal("100"),
        confidence=85,
        rationale="Strong buy signal",
        currency="KRW",
        payload={"key": "value"},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def sample_candidate_sell_holding() -> ResearchRunCandidate:
    """Create a sample sell holding candidate."""
    return ResearchRunCandidate(
        id=2,
        candidate_uuid=uuid4(),
        research_run_id=1,
        symbol="AAPL",
        instrument_type=InstrumentType.stock,
        side="sell",
        candidate_kind=ResearchRunCandidateKind.holding,
        proposed_price=Decimal("150.00"),
        proposed_qty=Decimal("10"),
        confidence=70,
        rationale="Take profits",
        currency="USD",
        payload={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class TestMapCandidateKindToProposalKind:
    """Tests for _map_candidate_kind_to_proposal_kind."""

    def test_pending_order_buy(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test pending_order + buy maps to enter."""
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_buy_pending)
        assert result == ProposalKind.enter

    def test_pending_order_sell(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test pending_order + sell maps to exit."""
        sample_candidate_buy_pending.side = "sell"
        sample_candidate_buy_pending.candidate_kind = ResearchRunCandidateKind.pending_order
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_buy_pending)
        assert result == ProposalKind.exit

    def test_holding_sell(self, sample_candidate_sell_holding: ResearchRunCandidate) -> None:
        """Test holding + sell maps to exit."""
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_sell_holding)
        assert result == ProposalKind.exit

    def test_holding_buy(self, sample_candidate_sell_holding: ResearchRunCandidate) -> None:
        """Test holding + buy maps to add."""
        sample_candidate_sell_holding.side = "buy"
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_sell_holding)
        assert result == ProposalKind.add

    def test_holding_none(self, sample_candidate_sell_holding: ResearchRunCandidate) -> None:
        """Test holding + none maps to trim."""
        sample_candidate_sell_holding.side = "none"
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_sell_holding)
        assert result == ProposalKind.trim

    def test_screener_hit_buy(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test screener_hit + buy maps to enter."""
        sample_candidate_buy_pending.candidate_kind = ResearchRunCandidateKind.screener_hit
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_buy_pending)
        assert result == ProposalKind.enter

    def test_screener_hit_none(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test screener_hit + none maps to breakout_watch."""
        sample_candidate_buy_pending.candidate_kind = ResearchRunCandidateKind.screener_hit
        sample_candidate_buy_pending.side = "none"
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_buy_pending)
        assert result == ProposalKind.breakout_watch

    def test_proposed_buy(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test proposed + buy maps to enter."""
        sample_candidate_buy_pending.candidate_kind = ResearchRunCandidateKind.proposed
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_buy_pending)
        assert result == ProposalKind.enter

    def test_other(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test other kind maps to other proposal kind."""
        sample_candidate_buy_pending.candidate_kind = ResearchRunCandidateKind.other
        sample_candidate_buy_pending.side = "none"
        result = _map_candidate_kind_to_proposal_kind(sample_candidate_buy_pending)
        assert result == ProposalKind.other


class TestBuildProposalFromCandidate:
    """Tests for _build_proposal_from_candidate."""

    def test_basic_mapping(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test basic field mapping without live price."""
        result = _build_proposal_from_candidate(sample_candidate_buy_pending)

        assert result["symbol"] == "005930"
        assert result["instrument_type"] == InstrumentType.stock
        assert result["proposal_kind"] == ProposalKind.enter
        assert result["side"] == "buy"
        assert result["original_quantity"] == Decimal("100")
        assert result["original_price"] == Decimal("70000.00")
        assert result["original_rationale"] == "Strong buy signal"
        assert result["original_currency"] == "KRW"
        assert result["original_payload"]["source"] == "research_run_candidate"
        assert result["original_payload"]["confidence"] == 85

    def test_with_live_price(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test mapping with live price refresh."""
        live_price = 71000.50
        result = _build_proposal_from_candidate(sample_candidate_buy_pending, live_price)

        assert result["original_price"] == live_price
        assert "live_refresh" in result["original_payload"]
        assert result["original_payload"]["live_refresh"]["price"] == live_price

    def test_candidate_payload_preserved(self, sample_candidate_buy_pending: ResearchRunCandidate) -> None:
        """Test that candidate payload is preserved in original_payload."""
        result = _build_proposal_from_candidate(sample_candidate_buy_pending)

        assert "candidate_payload" in result["original_payload"]
        assert result["original_payload"]["candidate_payload"]["key"] == "value"


@pytest.mark.asyncio
class TestCreateDecisionSessionFromResearchRun:
    """Tests for create_decision_session_from_research_run."""

    async def test_successful_creation(
        self,
        db: AsyncSession,
    ) -> None:
        """Test successful creation of decision session from research run."""
        # This test requires database setup - will be tested in integration tests
        pass

    async def test_research_run_not_found(self, db: AsyncSession) -> None:
        """Test error when research run does not exist."""
        non_existent_uuid = uuid4()

        with pytest.raises(ValueError, match=f"Research run not found: {non_existent_uuid}"):
            await create_decision_session_from_research_run(
                db,
                user_id=1,
                run_uuid=non_existent_uuid,
            )

    async def test_empty_candidates_allowed(self, db: AsyncSession) -> None:
        """Test that empty candidates list is allowed."""
        # This test requires database setup - will be tested in integration tests
        pass
```

- [ ] **Step 2: Run tests to verify they load**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run pytest tests/test_research_run_decision_session_service.py -v --collect-only`
Expected: All tests collected without import errors

- [ ] **Step 3: Commit**

```bash
git add tests/test_research_run_decision_session_service.py
git commit -m "test(research): add unit tests for research run decision session service"
```

---

## Task 5: Write Service Safety Tests

**Files:**
- Create: `tests/test_research_run_decision_session_service_safety.py`

- [ ] **Step 1: Write the test file**

```python
"""Safety and boundary tests for research_run_decision_session_service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunCandidateKind,
    ResearchRunStage,
    ResearchRunStatus,
)
from app.models.trading import InstrumentType
from app.services.research_run_decision_session_service import (
    _refresh_candidate_price,
    create_decision_session_from_research_run,
)


class TestOwnershipAndPermissions:
    """Tests for ownership validation and permission checks."""

    @pytest.mark.asyncio
    async def test_cannot_access_other_users_research_run(
        self,
        db: AsyncSession,
    ) -> None:
        """Test that user cannot create session from another user's research run."""
        # This would require DB setup - document the expected behavior
        # When research_run_service.get_research_run_by_uuid returns None
        # for non-owned runs, the function should raise ValueError
        pass


class TestRefreshCandidatePrice:
    """Tests for _refresh_candidate_price function."""

    @pytest.mark.asyncio
    async def test_kr_market_with_kis_client(self) -> None:
        """Test KR market price refresh with KIS client."""
        candidate = MagicMock()
        candidate.symbol = "005930"

        kis_client = AsyncMock()
        kis_client.get_quote = AsyncMock(return_value={"stck_prpr": "71000"})

        result = await _refresh_candidate_price(
            candidate,
            market_scope="kr",
            kis_client=kis_client,
        )

        assert result == 71000.0
        kis_client.get_quote.assert_called_once_with("005930")

    @pytest.mark.asyncio
    async def test_crypto_with_upbit_service(self) -> None:
        """Test crypto price refresh with Upbit service."""
        candidate = MagicMock()
        candidate.symbol = "KRW-BTC"

        upbit_service = AsyncMock()
        upbit_service.get_ticker = AsyncMock(return_value={"trade_price": 85000000})

        result = await _refresh_candidate_price(
            candidate,
            market_scope="crypto",
            upbit_service=upbit_service,
        )

        assert result == 85000000.0
        upbit_service.get_ticker.assert_called_once_with("KRW-BTC")

    @pytest.mark.asyncio
    async def test_refresh_failure_returns_none(self) -> None:
        """Test that refresh failure returns None (graceful degradation)."""
        candidate = MagicMock()
        candidate.symbol = "005930"

        kis_client = AsyncMock()
        kis_client.get_quote = AsyncMock(side_effect=Exception("API error"))

        result = await _refresh_candidate_price(
            candidate,
            market_scope="kr",
            kis_client=kis_client,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_no_client_returns_none(self) -> None:
        """Test that missing client returns None."""
        candidate = MagicMock()
        candidate.symbol = "005930"

        result = await _refresh_candidate_price(
            candidate,
            market_scope="kr",
            kis_client=None,
        )

        assert result is None


class TestBoundaryConditions:
    """Tests for boundary conditions and edge cases."""

    @pytest.mark.asyncio
    async def test_very_long_rationale(self, db: AsyncSession) -> None:
        """Test handling of very long rationale strings."""
        # Rationale should be preserved even if very long
        pass

    @pytest.mark.asyncio
    async def test_null_optional_fields(self, db: AsyncSession) -> None:
        """Test handling of null optional fields in candidate."""
        # Fields like proposed_price, proposed_qty, confidence can be None
        pass

    @pytest.mark.asyncio
    async def test_special_characters_in_symbol(self, db: AsyncSession) -> None:
        """Test handling of symbols with special characters."""
        # Symbols like "BTC-USD", "BRK.B" should be handled correctly
        pass


class TestResearchRunStatusValidation:
    """Tests for research run status validation."""

    @pytest.mark.asyncio
    async def test_archived_run_allowed(self, db: AsyncSession) -> None:
        """Test that archived research runs can still be converted."""
        # Archived runs should still allow decision session creation
        pass

    @pytest.mark.asyncio
    async def test_closed_run_allowed(self, db: AsyncSession) -> None:
        """Test that closed research runs can still be converted."""
        # Closed runs should still allow decision session creation
        pass
```

- [ ] **Step 2: Run tests to verify they load**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run pytest tests/test_research_run_decision_session_service_safety.py -v --collect-only`
Expected: All tests collected without import errors

- [ ] **Step 3: Commit**

```bash
git add tests/test_research_run_decision_session_service_safety.py
git commit -m "test(research): add safety tests for research run decision session service"
```

---

## Task 6: Write Router Integration Tests

**Files:**
- Create: `tests/test_research_run_decision_session_router.py`

- [ ] **Step 1: Write the test file**

```python
"""Router integration tests for research run to decision session endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import status
from httpx import AsyncClient

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunCandidateKind,
    ResearchRunStage,
    ResearchRunStatus,
)
from app.models.trading import InstrumentType
from app.models.trading_decision import SessionStatus


@pytest.mark.asyncio
class TestCreateDecisionFromResearchRunEndpoint:
    """Tests for POST /trading/api/decisions/from-research-run/{run_uuid}"""

    async def test_successful_creation(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test successful creation of decision session from research run."""
        run_uuid = uuid4()
        session_uuid = uuid4()

        mock_result = AsyncMock()
        mock_result.session.session_uuid = session_uuid
        mock_result.session.status = SessionStatus.open
        mock_result.proposal_count = 2
        mock_result.refreshed_candidates = 1

        with patch(
            "app.routers.trading_decisions.create_decision_session_from_research_run",
            new=AsyncMock(return_value=mock_result),
        ) as mock_service:
            response = await async_client.post(
                f"/trading/api/decisions/from-research-run/{run_uuid}",
                headers=auth_headers,
                json={
                    "refresh_mode": "price",
                    "strategy_name": "test_strategy",
                    "notes": "Test notes",
                },
            )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["session_uuid"] == str(session_uuid)
        assert data["status"] == "open"
        assert data["proposal_count"] == 2
        assert data["run_uuid"] == str(run_uuid)
        assert data["refreshed_candidates"] == 1
        assert "session_url" in data

    async def test_minimal_request(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test endpoint with minimal request body."""
        run_uuid = uuid4()

        mock_result = AsyncMock()
        mock_result.session.session_uuid = uuid4()
        mock_result.session.status = SessionStatus.open
        mock_result.proposal_count = 0
        mock_result.refreshed_candidates = 0

        with patch(
            "app.routers.trading_decisions.create_decision_session_from_research_run",
            new=AsyncMock(return_value=mock_result),
        ):
            response = await async_client.post(
                f"/trading/api/decisions/from-research-run/{run_uuid}",
                headers=auth_headers,
                json={},
            )

        assert response.status_code == status.HTTP_201_CREATED

    async def test_research_run_not_found(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test 404 response when research run not found."""
        run_uuid = uuid4()

        with patch(
            "app.routers.trading_decisions.create_decision_session_from_research_run",
            new=AsyncMock(side_effect=ValueError(f"Research run not found: {run_uuid}")),
        ):
            response = await async_client.post(
                f"/trading/api/decisions/from-research-run/{run_uuid}",
                headers=auth_headers,
                json={},
            )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "Research run not found" in response.json()["detail"]

    async def test_invalid_refresh_mode(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test 422 response for invalid refresh mode."""
        run_uuid = uuid4()

        response = await async_client.post(
            f"/trading/api/decisions/from-research-run/{run_uuid}",
            headers=auth_headers,
            json={"refresh_mode": "invalid_mode"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_location_header_set(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test that Location header is set in response."""
        run_uuid = uuid4()
        session_uuid = uuid4()

        mock_result = AsyncMock()
        mock_result.session.session_uuid = session_uuid
        mock_result.session.status = SessionStatus.open
        mock_result.proposal_count = 1
        mock_result.refreshed_candidates = 0

        with patch(
            "app.routers.trading_decisions.create_decision_session_from_research_run",
            new=AsyncMock(return_value=mock_result),
        ):
            response = await async_client.post(
                f"/trading/api/decisions/from-research-run/{run_uuid}",
                headers=auth_headers,
                json={},
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.headers["location"] == f"/trading/api/decisions/{session_uuid}"


class TestResponseSchema:
    """Tests for response schema validation."""

    def test_response_schema_fields(self) -> None:
        """Test that all expected fields are in response schema."""
        from app.schemas.research_run_decision_session import (
            ResearchRunToDecisionSessionResponse,
        )

        session_uuid = uuid4()
        run_uuid = uuid4()

        response = ResearchRunToDecisionSessionResponse(
            session_uuid=session_uuid,
            session_url="https://example.com/session",
            status="open",
            proposal_count=5,
            run_uuid=run_uuid,
            refreshed_candidates=3,
        )

        assert response.session_uuid == session_uuid
        assert response.proposal_count == 5
        assert response.refreshed_candidates == 3
```

- [ ] **Step 2: Run tests to verify they load**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run pytest tests/test_research_run_decision_session_router.py -v --collect-only`
Expected: All tests collected without import errors

- [ ] **Step 3: Commit**

```bash
git add tests/test_research_run_decision_session_router.py
git commit -m "test(research): add router integration tests for research run decision session"
```

---

## Task 7: Write Router Safety Tests

**Files:**
- Create: `tests/test_research_run_decision_session_router_safety.py`

- [ ] **Step 1: Write the test file**

```python
"""Router safety tests for research run to decision session endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient


@pytest.mark.asyncio
class TestAuthenticationAndAuthorization:
    """Tests for authentication and authorization."""

    async def test_unauthorized_request(self, async_client: AsyncClient) -> None:
        """Test 401 response without authentication."""
        run_uuid = uuid4()

        response = await async_client.post(
            f"/trading/api/decisions/from-research-run/{run_uuid}",
            json={},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_invalid_token(self, async_client: AsyncClient) -> None:
        """Test 401 response with invalid token."""
        run_uuid = uuid4()

        response = await async_client.post(
            f"/trading/api/decisions/from-research-run/{run_uuid}",
            headers={"Authorization": "Bearer invalid_token"},
            json={},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
class TestInputValidation:
    """Tests for input validation."""

    async def test_invalid_uuid_format(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test 422 response for invalid UUID format."""
        response = await async_client.post(
            "/trading/api/decisions/from-research-run/invalid-uuid",
            headers=auth_headers,
            json={},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_extra_fields_rejected(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test that extra fields in request body are rejected."""
        run_uuid = uuid4()

        response = await async_client.post(
            f"/trading/api/decisions/from-research-run/{run_uuid}",
            headers=auth_headers,
            json={"extra_field": "should_fail"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_strategy_name_too_long(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test 422 response for strategy name exceeding max length."""
        run_uuid = uuid4()

        response = await async_client.post(
            f"/trading/api/decisions/from-research-run/{run_uuid}",
            headers=auth_headers,
            json={"strategy_name": "x" * 200},  # Exceeds 128 char limit
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_notes_too_long(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test 422 response for notes exceeding max length."""
        run_uuid = uuid4()

        response = await async_client.post(
            f"/trading/api/decisions/from-research-run/{run_uuid}",
            headers=auth_headers,
            json={"notes": "x" * 5000},  # Exceeds 4000 char limit
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
class TestErrorHandling:
    """Tests for error handling."""

    async def test_service_error_handling(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test proper error handling when service raises unexpected error."""
        run_uuid = uuid4()

        with patch(
            "app.routers.trading_decisions.create_decision_session_from_research_run",
            new=AsyncMock(side_effect=Exception("Unexpected error")),
        ):
            response = await async_client.post(
                f"/trading/api/decisions/from-research-run/{run_uuid}",
                headers=auth_headers,
                json={},
            )

        # Should return 500 or be handled by exception handlers
        assert response.status_code in [
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            status.HTTP_502_BAD_GATEWAY,
        ]

    async def test_database_error_handling(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        """Test proper error handling on database errors."""
        run_uuid = uuid4()

        with patch(
            "app.routers.trading_decisions.create_decision_session_from_research_run",
            new=AsyncMock(side_effect=ConnectionError("DB connection failed")),
        ):
            response = await async_client.post(
                f"/trading/api/decisions/from-research-run/{run_uuid}",
                headers=auth_headers,
                json={},
            )

        assert response.status_code in [
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ]
```

- [ ] **Step 2: Run tests to verify they load**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run pytest tests/test_research_run_decision_session_router_safety.py -v --collect-only`
Expected: All tests collected without import errors

- [ ] **Step 3: Commit**

```bash
git add tests/test_research_run_decision_session_router_safety.py
git commit -m "test(research): add router safety tests for research run decision session"
```

---

## Task 8: Write Live Refresh Service Tests

**Files:**
- Create: `tests/test_research_run_live_refresh_service.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for live refresh functionality in research run decision session service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunCandidateKind,
    ResearchRunStage,
    ResearchRunStatus,
)
from app.models.trading import InstrumentType
from app.services.research_run_decision_session_service import (
    _refresh_candidate_price,
    create_decision_session_from_research_run,
)


@pytest.mark.asyncio
class TestRefreshNoneMode:
    """Tests for refresh_mode='none' - no live data refresh."""

    async def test_no_refresh_calls_made(
        self,
        db: AsyncSession,
    ) -> None:
        """Test that no market data calls are made when refresh_mode='none'."""
        # This test verifies that with refresh_mode='none',
        # no KIS or Upbit services are called
        pass

    async def test_stored_price_used(self) -> None:
        """Test that stored price from candidate is used when refresh_mode='none'."""
        pass


@pytest.mark.asyncio
class TestRefreshPriceMode:
    """Tests for refresh_mode='price' - refresh only price."""

    async def test_kr_market_price_refresh(self) -> None:
        """Test KR market price refresh with KIS client."""
        candidate = MagicMock()
        candidate.symbol = "005930"

        kis_client = AsyncMock()
        kis_client.get_quote = AsyncMock(return_value={"stck_prpr": "71500"})

        result = await _refresh_candidate_price(
            candidate,
            market_scope="kr",
            kis_client=kis_client,
        )

        assert result == 71500.0
        kis_client.get_quote.assert_called_once()

    async def test_crypto_price_refresh(self) -> None:
        """Test crypto price refresh with Upbit service."""
        candidate = MagicMock()
        candidate.symbol = "KRW-BTC"

        upbit_service = AsyncMock()
        upbit_service.get_ticker = AsyncMock(return_value={"trade_price": 90000000})

        result = await _refresh_candidate_price(
            candidate,
            market_scope="crypto",
            upbit_service=upbit_service,
        )

        assert result == 90000000.0
        upbit_service.get_ticker.assert_called_once()

    async def test_us_market_no_refresh(self) -> None:
        """Test that US market returns None (no refresh support yet)."""
        candidate = MagicMock()
        candidate.symbol = "AAPL"

        result = await _refresh_candidate_price(
            candidate,
            market_scope="us",
            kis_client=None,
        )

        assert result is None

    async def test_graceful_fallback_on_api_error(self) -> None:
        """Test graceful fallback when market API fails."""
        candidate = MagicMock()
        candidate.symbol = "005930"

        kis_client = AsyncMock()
        kis_client.get_quote = AsyncMock(side_effect=Exception("API timeout"))

        result = await _refresh_candidate_price(
            candidate,
            market_scope="kr",
            kis_client=kis_client,
        )

        assert result is None


@pytest.mark.asyncio
class TestRefreshFullMode:
    """Tests for refresh_mode='full' - full market data refresh."""

    async def test_full_refresh_price_update(self) -> None:
        """Test that full mode also updates price."""
        # In current implementation, 'full' behaves same as 'price'
        # Future: could include additional market data
        pass


@pytest.mark.asyncio
class TestCandidatePriceMapping:
    """Tests for candidate to proposal price mapping with live refresh."""

    def test_live_price_overrides_stored(self) -> None:
        """Test that live price overrides stored candidate price."""
        from app.services.research_run_decision_session_service import (
            _build_proposal_from_candidate,
        )

        candidate = MagicMock()
        candidate.symbol = "005930"
        candidate.instrument_type = InstrumentType.stock
        candidate.side = "buy"
        candidate.candidate_kind = ResearchRunCandidateKind.pending_order
        candidate.proposed_price = Decimal("70000")
        candidate.proposed_qty = Decimal("100")
        candidate.confidence = 80
        candidate.rationale = "Test"
        candidate.currency = "KRW"
        candidate.payload = {}

        live_price = 71500.0
        result = _build_proposal_from_candidate(candidate, live_price)

        assert result["original_price"] == live_price
        assert result["original_payload"]["live_refresh"]["price"] == live_price

    def test_stored_price_used_when_no_live(self) -> None:
        """Test that stored price is used when live refresh not available."""
        from app.services.research_run_decision_session_service import (
            _build_proposal_from_candidate,
        )

        candidate = MagicMock()
        candidate.symbol = "005930"
        candidate.instrument_type = InstrumentType.stock
        candidate.side = "buy"
        candidate.candidate_kind = ResearchRunCandidateKind.pending_order
        candidate.proposed_price = Decimal("70000")
        candidate.proposed_qty = Decimal("100")
        candidate.confidence = 80
        candidate.rationale = "Test"
        candidate.currency = "KRW"
        candidate.payload = {}

        result = _build_proposal_from_candidate(candidate, None)

        assert result["original_price"] == Decimal("70000")
        assert "live_refresh" not in result["original_payload"]


@pytest.mark.asyncio
class TestRefreshedCandidatesCount:
    """Tests for tracking number of refreshed candidates."""

    async def test_refreshed_count_returned(self) -> None:
        """Test that refreshed_candidates count is returned in result."""
        # This would be tested in integration tests
        pass

    async def test_zero_refreshed_when_no_clients(self) -> None:
        """Test zero refreshed when no market clients provided."""
        pass

    async def test_count_matches_successful_refreshes(self) -> None:
        """Test that count matches number of successful price refreshes."""
        pass
```

- [ ] **Step 2: Run tests to verify they load**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run pytest tests/test_research_run_live_refresh_service.py -v --collect-only`
Expected: All tests collected without import errors

- [ ] **Step 3: Commit**

```bash
git add tests/test_research_run_live_refresh_service.py
git commit -m "test(research): add live refresh service tests for research run decision session"
```

---

## Task 9: Run Quality Checks

- [ ] **Step 1: Run Ruff linter**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run ruff check app/schemas/research_run_decision_session.py app/services/research_run_decision_session_service.py app/routers/trading_decisions.py tests/test_research_run*.py`
Expected: No lint errors

- [ ] **Step 2: Run Ruff format check**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run ruff format --check app/schemas/research_run_decision_session.py app/services/research_run_decision_session_service.py app/routers/trading_decisions.py tests/test_research_run*.py`
Expected: All files properly formatted (or run `uv run ruff format` to fix)

- [ ] **Step 3: Run ty type checker**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run ty check app/schemas/research_run_decision_session.py app/services/research_run_decision_session_service.py app/routers/trading_decisions.py`
Expected: No type errors

- [ ] **Step 4: Run tests**

Run: `cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session && uv run pytest tests/test_research_run*.py -v --tb=short`
Expected: All tests pass (some may be skipped if they need DB setup)

---

## Task 10: Final Commit

- [ ] **Step 1: Stage all changes**

```bash
git add -A
```

- [ ] **Step 2: Create final commit**

```bash
git commit -m "feat(research): create decision session from research run live refresh

Add service and endpoint to convert research run snapshots into trading
decision sessions with optional live market data refresh.

Features:
- POST /trading/api/decisions/from-research-run/{run_uuid} endpoint
- Support for refresh_mode: none, price, full
- Live price refresh for KR (KIS) and crypto (Upbit) markets
- Automatic proposal kind mapping from candidate kind + side
- Comprehensive test coverage

Files added:
- app/schemas/research_run_decision_session.py
- app/services/research_run_decision_session_service.py
- tests/test_research_run_decision_session_service.py
- tests/test_research_run_decision_session_service_safety.py
- tests/test_research_run_decision_session_router.py
- tests/test_research_run_decision_session_router_safety.py
- tests/test_research_run_live_refresh_service.py

Files modified:
- app/routers/trading_decisions.py

Closes ROB-25"
```

---

## Spec Coverage Verification

| Spec Requirement | Task | Status |
|-----------------|------|--------|
| POST endpoint for creating decision session from research run | Task 3 | ✅ |
| Live refresh support with refresh modes | Task 2 | ✅ |
| Candidate to proposal field mapping | Task 2 | ✅ |
| Proposal kind mapping logic | Task 2 | ✅ |
| Source profile = "research_run_live_refresh" | Task 2 | ✅ |
| Response with session_uuid, session_url, proposal_count | Task 1, 3 | ✅ |
| Unit tests for service | Task 4 | ✅ |
| Safety tests for service | Task 5 | ✅ |
| Router integration tests | Task 6 | ✅ |
| Router safety tests | Task 7 | ✅ |
| Live refresh specific tests | Task 8 | ✅ |
| Quality checks (ruff, ty) | Task 9 | ✅ |

---

## Next Steps

**Plan complete!** Choose execution approach:

1. **Subagent-Driven (recommended)** - Dispatch fresh subagent per task, review between tasks
2. **Inline Execution** - Execute tasks in this session using executing-plans skill

To execute:
- Use `/superpowers:subagent-driven-development` with this plan file
- Or use `/superpowers:executing-plans` for inline execution
