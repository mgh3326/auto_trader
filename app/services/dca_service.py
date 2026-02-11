"""
DCA (Dollar Cost Averaging) Service

DCA 플랜 생성, 조회, 상태 업데이트를 담당하는 서비스
"""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dca_plan import (
    DcaPlan,
    DcaPlanStatus,
    DcaPlanStep,
    DcaStepStatus,
)

logger = logging.getLogger(__name__)


class DcaService:
    """DCA 플랜 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        """Convert numeric input to Decimal for consistent precision."""
        return Decimal(str(value))

    async def create_plan(
        self,
        user_id: int,
        symbol: str,
        market: str,
        total_amount: Decimal | float,
        splits: int,
        strategy: str,
        plans_data: list[dict[str, Any]],
        rsi_14: float | None = None,
    ) -> DcaPlan:
        """DCA 플랜 생성 (플랜 + 단계 일괄 생성)"""
        plan = DcaPlan(
            user_id=user_id,
            symbol=symbol,
            market=market,
            total_amount=self._to_decimal(total_amount),
            splits=splits,
            strategy=strategy,
            rsi_14=Decimal(str(rsi_14)) if rsi_14 is not None else None,
        )
        self.db.add(plan)

        # Flush to get plan.id for FK relationships
        await self.db.flush()

        steps = []
        for plan_data in plans_data:
            step = DcaPlanStep(
                plan_id=plan.id,
                step_number=plan_data["step"],
                target_price=self._to_decimal(plan_data["price"]),
                target_amount=self._to_decimal(plan_data["amount"]),
                target_quantity=self._to_decimal(plan_data.get("quantity", 0)),
                level_source=plan_data.get("source"),
            )
            self.db.add(step)
            steps.append(step)

        await self.db.commit()

        steps_to_refresh = steps
        await self.db.refresh(plan)
        for step in steps_to_refresh:
            await self.db.refresh(step)

        logger.info(
            f"Created DCA plan: id={plan.id}, user_id={user_id}, "
            f"symbol={symbol}, splits={splits}"
        )
        return plan

    async def get_plan(
        self, plan_id: int, user_id: int | None = None
    ) -> DcaPlan | None:
        """ID로 DCA 플랜 조회 (단계 eager load)"""
        query = select(DcaPlan).where(DcaPlan.id == plan_id)

        if user_id is not None:
            query = query.where(DcaPlan.user_id == user_id)

        result = await self.db.execute(query.options(selectinload(DcaPlan.steps)))
        return result.scalar_one_or_none()

    async def get_plans_by_status(
        self,
        user_id: int,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 20,
    ) -> list[DcaPlan]:
        """상태/종목 조건으로 DCA 플랜 목록 조회"""
        query = select(DcaPlan).where(DcaPlan.user_id == user_id)

        if status is not None:
            # Convert string status to enum
            try:
                status_enum = DcaPlanStatus(status)
                query = query.where(DcaPlan.status == status_enum)
            except ValueError:
                raise ValueError(
                    f"Invalid status '{status}'. Must be one of: "
                    f"{', '.join([s.value for s in DcaPlanStatus])}"
                )

        if symbol is not None:
            query = query.where(DcaPlan.symbol == symbol)

        result = await self.db.execute(
            query.order_by(DcaPlan.created_at.desc())
            .limit(limit)
            .options(selectinload(DcaPlan.steps))
        )
        return list(result.scalars().all())

    async def mark_step_ordered(
        self, step_id: int, order_id: str | None = None
    ) -> DcaPlanStep | None:
        """단계를 'ordered' 상태로 업데이트"""
        import datetime

        step_result = await self.db.execute(
            select(DcaPlanStep).where(DcaPlanStep.id == step_id)
        )
        step = step_result.scalar_one_or_none()

        if not step:
            return None

        step.status = DcaStepStatus.ORDERED
        step.order_id = order_id

        # Use UTC timezone instead of settings.timezone
        now = datetime.datetime.now(datetime.UTC)
        step.ordered_at = now

        await self.db.commit()
        await self.db.refresh(step)

        logger.info(f"Marked DCA step ordered: id={step_id}, order_id={order_id}")
        return step

    async def mark_step_filled(
        self,
        step_id: int,
        filled_price: Decimal | float,
        filled_qty: Decimal | float,
        filled_amount: Decimal | float | None = None,
    ) -> DcaPlanStep | None:
        """단계 체결 정보 기록 및 플랜 완료 체크"""
        import datetime

        step_result = await self.db.execute(
            select(DcaPlanStep).where(DcaPlanStep.id == step_id)
        )
        step = step_result.scalar_one_or_none()

        if not step:
            return None

        step.status = DcaStepStatus.FILLED
        step.filled_price = self._to_decimal(filled_price)
        step.filled_quantity = self._to_decimal(filled_qty)

        step.filled_amount = (
            self._to_decimal(filled_amount)
            if filled_amount is not None
            else self._to_decimal(filled_price) * self._to_decimal(filled_qty)
        )

        # Use UTC timezone
        now = datetime.datetime.now(datetime.UTC)
        step.filled_at = now

        await self.db.commit()
        await self.db.refresh(step)

        await self._check_plan_completion(step.plan_id)

        logger.info(
            f"Marked DCA step filled: id={step_id}, "
            f"price={filled_price}, qty={filled_qty}"
        )
        return step

    async def mark_step_cancelled(self, step_id: int) -> DcaPlanStep | None:
        """단계 취소"""
        step_result = await self.db.execute(
            select(DcaPlanStep).where(DcaPlanStep.id == step_id)
        )
        step = step_result.scalar_one_or_none()

        if not step:
            return None

        step.status = DcaStepStatus.CANCELLED
        await self.db.commit()
        await self.db.refresh(step)

        logger.info(f"Marked DCA step cancelled: id={step_id}")
        return step

    async def cancel_plan(self, plan_id: int, user_id: int) -> DcaPlan | None:
        """DCA 플랜 취소 (pending|ordered|partial 단계만 cancelled)"""
        plan = await self.get_plan(plan_id, user_id)
        if not plan:
            return None

        plan.status = DcaPlanStatus.CANCELLED

        query = select(DcaPlanStep).where(
            and_(
                DcaPlanStep.plan_id == plan_id,
                or_(
                    DcaPlanStep.status == DcaStepStatus.PENDING,
                    DcaPlanStep.status == DcaStepStatus.ORDERED,
                    DcaPlanStep.status == DcaStepStatus.PARTIAL,
                ),
            )
        )

        result = await self.db.execute(query)
        steps_to_cancel = list(result.scalars().all())

        for step in steps_to_cancel:
            step.status = DcaStepStatus.CANCELLED

        await self.db.commit()
        await self.db.refresh(plan)

        logger.info(
            f"Cancelled DCA plan: id={plan_id}, steps_cancelled={len(steps_to_cancel)}"
        )
        return plan

    async def find_step_by_order_id(self, order_id: str) -> DcaPlanStep | None:
        """order_id로 단계 조회"""
        result = await self.db.execute(
            select(DcaPlanStep)
            .where(DcaPlanStep.order_id == order_id)
            .options(selectinload(DcaPlanStep.plan))
        )
        return result.scalar_one_or_none()

    async def get_next_pending_step(self, plan_id: int) -> DcaPlanStep | None:
        """플랜의 다음 대기 단계 조회"""
        result = await self.db.execute(
            select(DcaPlanStep)
            .where(
                and_(
                    DcaPlanStep.plan_id == plan_id,
                    DcaPlanStep.status == DcaStepStatus.PENDING,
                )
            )
            .order_by(DcaPlanStep.step_number)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _check_plan_completion(self, plan_id: int) -> None:
        """플랜 자동 완료 체크 (active 상태일 때만)"""
        import datetime

        plan = await self.db.execute(select(DcaPlan).where(DcaPlan.id == plan_id))
        plan = plan.scalar_one_or_none()

        if not plan or plan.status != DcaPlanStatus.ACTIVE:
            return

        query = select(DcaPlanStep).where(
            and_(
                DcaPlanStep.plan_id == plan_id,
                or_(
                    DcaPlanStep.status == DcaStepStatus.PENDING,
                    DcaPlanStep.status == DcaStepStatus.ORDERED,
                    DcaPlanStep.status == DcaStepStatus.PARTIAL,
                ),
            )
        )

        result = await self.db.execute(query)
        active_step_count = len(list(result.scalars().all()))

        if active_step_count == 0:
            plan.status = DcaPlanStatus.COMPLETED

            # Use UTC timezone
            now = datetime.datetime.now(datetime.UTC)
            plan.completed_at = now

            await self.db.commit()

            logger.info(f"Auto-completed DCA plan: id={plan_id}")
