"""
Broker Account Service

브로커 계좌 관리 서비스
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import BrokerAccount, BrokerType, MarketType

logger = logging.getLogger(__name__)


class BrokerAccountService:
    """브로커 계좌 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_account(
        self,
        user_id: int,
        broker_type: BrokerType,
        account_name: str = "기본 계좌",
        is_mock: bool = False,
    ) -> BrokerAccount:
        """새 브로커 계좌 생성"""
        account = BrokerAccount(
            user_id=user_id,
            broker_type=broker_type,
            account_name=account_name,
            is_mock=is_mock,
            is_active=True,
        )
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        logger.info(
            f"Created broker account: user_id={user_id}, "
            f"broker={broker_type}, name={account_name}"
        )
        return account

    async def get_accounts(self, user_id: int) -> list[BrokerAccount]:
        """사용자의 모든 브로커 계좌 조회"""
        result = await self.db.execute(
            select(BrokerAccount)
            .where(BrokerAccount.user_id == user_id)
            .where(BrokerAccount.is_active.is_(True))
            .order_by(BrokerAccount.broker_type, BrokerAccount.account_name)
        )
        return list(result.scalars().all())

    async def get_account_by_id(
        self, account_id: int, include_inactive: bool = False
    ) -> BrokerAccount | None:
        """ID로 브로커 계좌 조회"""
        query = select(BrokerAccount).where(BrokerAccount.id == account_id)
        if not include_inactive:
            query = query.where(BrokerAccount.is_active.is_(True))

        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_account_by_user_and_broker(
        self,
        user_id: int,
        broker_type: BrokerType,
        account_name: str = "기본 계좌",
        include_inactive: bool = False,
    ) -> BrokerAccount | None:
        """사용자와 브로커 타입으로 계좌 조회"""
        query = (
            select(BrokerAccount)
            .where(BrokerAccount.user_id == user_id)
            .where(BrokerAccount.broker_type == broker_type)
            .where(BrokerAccount.account_name == account_name)
        )
        if not include_inactive:
            query = query.where(BrokerAccount.is_active.is_(True))

        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_or_create_default_account(
        self, user_id: int, broker_type: BrokerType
    ) -> BrokerAccount:
        """기본 계좌 조회 또는 생성"""
        account = await self.get_account_by_user_and_broker(
            user_id, broker_type, "기본 계좌"
        )
        if account:
            return account
        try:
            return await self.create_account(user_id, broker_type, "기본 계좌")
        except IntegrityError:
            await self.db.rollback()
            logger.info(
                "Default account already exists after race, returning existing account"
            )
            existing_account = await self.get_account_by_user_and_broker(
                user_id, broker_type, "기본 계좌"
            )
            if existing_account:
                return existing_account
            raise

    async def update_account(self, account_id: int, **kwargs) -> BrokerAccount | None:
        """브로커 계좌 업데이트"""
        account = await self.get_account_by_id(account_id)
        if not account:
            return None

        for key, value in kwargs.items():
            if hasattr(account, key) and key not in ("id", "user_id", "created_at"):
                setattr(account, key, value)

        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def delete_account(self, account_id: int) -> bool:
        """브로커 계좌 삭제 (soft delete)"""
        account = await self.get_account_by_id(account_id)
        if not account:
            return False

        account.is_active = False
        await self.db.commit()
        logger.info(f"Soft deleted broker account: id={account_id}")
        return True

    async def hard_delete_account(self, account_id: int) -> bool:
        """브로커 계좌 완전 삭제"""
        account = await self.get_account_by_id(account_id, include_inactive=True)
        if not account:
            return False

        await self.db.delete(account)
        await self.db.commit()
        logger.info(f"Hard deleted broker account: id={account_id}")
        return True
