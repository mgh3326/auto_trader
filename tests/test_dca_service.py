"""DCA Service Tests

Tests for DcaService including:
- create_plan
- mark_step_ordered
- mark_step_filled
- mark_step_cancelled
- cancel_plan
- _check_plan_completion
- find_step_by_order_id
- get_next_pending_step
- get_plans_by_status
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dca_plan import (
    DcaPlan,
    DcaPlanStatus,
    DcaPlanStep,
    DcaStepStatus,
)
from app.services.dca_service import DcaService


@pytest.fixture
def mock_db():
    """Mock database session."""
    return AsyncMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_create_plan_creates_plan_and_steps(mock_db):
    """Test that create_plan creates a DCA plan with steps."""
    # Setup
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock(return_value=None)
    mock_db.commit = AsyncMock(return_value=None)
    mock_db.refresh = AsyncMock(return_value=None)

    service = DcaService(mock_db)

    # Create test data
    plans_data = [
        {
            "step": 1,
            "price": Decimal("50000"),
            "amount": Decimal("100000"),
            "quantity": Decimal("2"),
            "source": "support",
        },
    ]

    # Execute
    result = await service.create_plan(
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        total_amount=Decimal("100000"),
        splits=1,
        strategy="support",
        plans_data=plans_data,
        rsi_14=45.0,
    )

    # Verify
    assert mock_db.add.called
    assert mock_db.flush.called
    assert mock_db.commit.called
    assert result is not None


@pytest.mark.asyncio
async def test_mark_step_filled_not_found(mock_db):
    """Test that mark_step_filled returns None when step not found."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.mark_step_filled(
        step_id=999,
        filled_price=Decimal("49500"),
        filled_qty=Decimal("0.002"),
    )

    # Verify
    assert result is None


@pytest.mark.asyncio
async def test_mark_step_ordered_updates_status(mock_db):
    """Test that mark_step_ordered updates status and order_id correctly."""
    # Setup
    mock_step = DcaPlanStep(
        id=1,
        plan_id=100,
        step_number=1,
        target_price=Decimal("50000"),
        target_amount=Decimal("100000"),
        target_quantity=Decimal("0.002"),
        status=DcaStepStatus.PENDING,
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_step
    mock_db.execute.return_value = mock_result
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    service = DcaService(mock_db)

    # Execute
    result = await service.mark_step_ordered(1, "ORDER-123")

    # Verify
    assert result is not None
    assert result.status == DcaStepStatus.ORDERED
    assert result.order_id == "ORDER-123"
    mock_db.commit.assert_awaited_once()
    mock_db.refresh.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_mark_step_cancelled_updates_status(mock_db):
    """Test that mark_step_cancelled updates status correctly."""
    # Setup
    mock_step = DcaPlanStep(
        id=1,
        plan_id=100,
        step_number=1,
        target_price=Decimal("50000"),
        target_amount=Decimal("100000"),
        target_quantity=Decimal("0.002"),
        status=DcaStepStatus.PENDING,
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_step
    mock_db.execute.return_value = mock_result
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    service = DcaService(mock_db)

    # Execute
    result = await service.mark_step_cancelled(1)

    # Verify
    assert result is not None
    assert result.status == DcaStepStatus.CANCELLED
    mock_db.commit.assert_awaited_once()
    mock_db.refresh.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_cancel_plan_not_found(mock_db):
    """Test that cancel_plan returns None when plan not found."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.cancel_plan(999, user_id=1)

    # Verify
    assert result is None


@pytest.mark.asyncio
async def test_find_step_by_order_id(mock_db):
    """Test that find_step_by_order_id returns step with eager-loaded plan."""
    # Setup
    mock_step = DcaPlanStep(
        id=1,
        plan_id=100,
        step_number=1,
        order_id="ORDER-123",
        status=DcaStepStatus.ORDERED,
    )

    mock_plan_result = MagicMock()
    mock_plan_result.scalar_one_or_none.return_value = mock_step

    # Mock result with eager-loaded steps
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_step

    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.find_step_by_order_id("ORDER-123")

    # Verify
    assert result is not None
    assert result.order_id == "ORDER-123"


@pytest.mark.asyncio
async def test_find_step_by_order_id_not_found(mock_db):
    """Test that find_step_by_order_id returns None when not found."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.find_step_by_order_id("NOT-FOUND")

    # Verify
    assert result is None


@pytest.mark.asyncio
async def test_get_next_pending_step(mock_db):
    """Test that get_next_pending_step returns the next pending step."""
    # Setup
    _ = DcaPlanStep(
        id=1,
        plan_id=100,
        step_number=1,
        status=DcaStepStatus.ORDERED,
    )
    step2 = DcaPlanStep(
        id=2,
        plan_id=100,
        step_number=2,
        status=DcaStepStatus.PENDING,
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = step2
    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_next_pending_step(100)

    # Verify
    assert result is not None
    assert result.id == 2
    assert result.status == DcaStepStatus.PENDING


@pytest.mark.asyncio
async def test_get_next_pending_step_no_pending(mock_db):
    """Test that get_next_pending_step returns None when no pending steps."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_next_pending_step(100)

    # Verify
    assert result is None


@pytest.mark.asyncio
async def test_get_plan_with_user_id(mock_db):
    """Test that get_plan returns plan with eager-loaded steps."""
    # Setup
    mock_plan = DcaPlan(
        id=100,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_plan

    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_plan(100, user_id=1)

    # Verify
    assert result is not None
    assert result.id == 100


@pytest.mark.asyncio
async def test_get_plan_without_user_id(mock_db):
    """Test that get_plan returns plan without user filtering."""
    # Setup
    mock_plan = DcaPlan(
        id=100,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_plan

    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_plan(100)

    # Verify
    assert result is not None
    assert result.id == 100


@pytest.mark.asyncio
async def test_get_plan_not_found(mock_db):
    """Test that get_plan returns None when plan not found."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_plan(999)

    # Verify
    assert result is None


@pytest.mark.asyncio
async def test_get_plans_by_status_filters_by_status(mock_db):
    """Test that get_plans_by_status filters by status."""
    # Setup
    plan1 = DcaPlan(
        id=1,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )
    _ = DcaPlan(
        id=2,
        user_id=1,
        symbol="KRW-ETH",
        market="crypto",
        status=DcaPlanStatus.COMPLETED,
    )

    mock_result = MagicMock()
    mock_result.scalars().all.return_value = [plan1]
    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_plans_by_status(
        user_id=1,
        status="active",
    )

    # Verify
    assert len(result) == 1
    assert result[0].id == 1
    assert result[0].status == DcaPlanStatus.ACTIVE


@pytest.mark.asyncio
async def test_get_plans_by_status_filters_by_symbol(mock_db):
    """Test that get_plans_by_status filters by symbol."""
    # Setup
    plan1 = DcaPlan(
        id=1,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )
    plan2 = DcaPlan(
        id=2,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.COMPLETED,
    )

    mock_result = MagicMock()
    mock_result.scalars().all.return_value = [plan1, plan2]

    mock_db.execute.return_value = mock_result

    service = DcaService(mock_db)

    # Execute
    result = await service.get_plans_by_status(
        user_id=1,
        symbol="KRW-BTC",
    )

    # Verify
    assert len(result) == 2
    assert result[0].id == 1
    assert result[1].id == 2

    # (N+1 방지용 selectinload 사용 여부는 내부 구현 디테일이므로,
    #  여기서는 반환된 결과와 필터 동작만 검증하고 ORM 옵션 자체는 검증하지 않는다.)


@pytest.mark.asyncio
async def test_cancel_plan_cancels_pending_steps(mock_db):
    """Test that cancel_plan marks pending/ordered/partial steps as cancelled."""
    # Setup - Mock plan
    mock_plan = DcaPlan(
        id=100,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )

    # Mock steps (some pending, some ordered, some already filled)
    step1 = DcaPlanStep(
        id=1,
        plan_id=100,
        step_number=1,
        status=DcaStepStatus.PENDING,
    )
    step2 = DcaPlanStep(
        id=2,
        plan_id=100,
        step_number=2,
        status=DcaStepStatus.ORDERED,
    )
    step3 = DcaPlanStep(
        id=3,
        plan_id=100,
        step_number=3,
        status=DcaStepStatus.FILLED,
    )

    # Mock get_plan to return plan with steps
    async def mock_get_plan(plan_id, user_id=None):
        return mock_plan

    # Mock execute for steps cancellation query
    mock_steps_result = MagicMock()
    mock_steps_result.scalars().all.return_value = [step1, step2]

    service = DcaService(mock_db)
    service.get_plan = mock_get_plan
    mock_db.execute.return_value = mock_steps_result
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    # Execute
    result = await service.cancel_plan(100, user_id=1)

    # Verify
    assert result is not None
    assert result.status == DcaPlanStatus.CANCELLED
    # Verify that pending/ordered steps were cancelled
    assert step1.status == DcaStepStatus.CANCELLED
    assert step2.status == DcaStepStatus.CANCELLED
    # Verify that already filled step remains filled
    assert step3.status == DcaStepStatus.FILLED
    mock_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_plan_completion_marks_plan_completed(mock_db):
    """Test that _check_plan_completion marks plan as completed when all steps are filled/cancelled."""
    # Setup - Mock plan with ACTIVE status
    mock_plan = DcaPlan(
        id=100,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )

    # Mock execute to return plan
    mock_plan_result = MagicMock()
    mock_plan_result.scalar_one_or_none.return_value = mock_plan

    # Mock execute to return active steps (should be empty)
    mock_steps_result = MagicMock()
    mock_steps_result.scalars().all.return_value = []  # No active steps

    # Set up sequential returns
    mock_db.execute.side_effect = [mock_plan_result, mock_steps_result]
    mock_db.commit = AsyncMock()

    service = DcaService(mock_db)

    # Execute
    await service._check_plan_completion(100)

    # Verify
    assert mock_plan.status == DcaPlanStatus.COMPLETED
    assert mock_plan.completed_at is not None
    mock_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_step_filled_triggers_plan_completion(mock_db):
    """Test that mark_step_filled triggers plan completion when all steps are filled."""
    # Setup - Mock step
    mock_step = DcaPlanStep(
        id=1,
        plan_id=100,
        step_number=1,
        target_price=Decimal("50000"),
        target_amount=Decimal("100000"),
        target_quantity=Decimal("0.002"),
        status=DcaStepStatus.PENDING,
    )

    # Mock plan with ACTIVE status
    mock_plan = DcaPlan(
        id=100,
        user_id=1,
        symbol="KRW-BTC",
        market="crypto",
        status=DcaPlanStatus.ACTIVE,
    )

    # Mock execute to return step
    mock_step_result = MagicMock()
    mock_step_result.scalar_one_or_none.return_value = mock_step

    # Mock execute for plan completion check (empty active steps)
    mock_plan_result = MagicMock()
    mock_plan_result.scalar_one_or_none.return_value = mock_plan

    mock_active_steps_result = MagicMock()
    mock_active_steps_result.scalars().all.return_value = []  # No active steps

    # Set up sequential returns: step query, plan query, active steps query
    mock_db.execute.side_effect = [
        mock_step_result,
        mock_plan_result,
        mock_active_steps_result,
    ]
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    service = DcaService(mock_db)

    # Execute
    result = await service.mark_step_filled(
        step_id=1,
        filled_price=Decimal("49500"),
        filled_qty=Decimal("0.002"),
    )

    # Verify step was filled
    assert result is not None
    assert result.status == DcaStepStatus.FILLED
    assert result.filled_price == Decimal("49500")
    assert result.filled_quantity == Decimal("0.002")
    # Verify filled_amount was auto-calculated
    assert result.filled_amount == Decimal("49500") * Decimal("0.002")

    # Verify plan completion was triggered
    assert mock_plan.status == DcaPlanStatus.COMPLETED
    assert mock_plan.completed_at is not None
    mock_db.commit.assert_awaited()  # Should be called at least once
