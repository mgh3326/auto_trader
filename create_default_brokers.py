"""기본 증권사 계정 생성 스크립트"""
import asyncio
from app.core.db import AsyncSessionLocal
from app.models.trading import BrokerAccount, User
from sqlalchemy import select


async def create_default_brokers():
    """기본 증권사 계정 생성"""
    async with AsyncSessionLocal() as db:
        # user_id=1 확인
        result = await db.execute(select(User).where(User.id == 1))
        user = result.scalar_one_or_none()

        if not user:
            print("✗ User ID 1 not found. Please run create_default_user.py first.")
            return

        print(f"✓ User found: {user.nickname} ({user.email})")

        # 기본 증권사 계정 정의
        default_brokers = [
            {
                "broker_type": "kis",
                "broker_name": "한국투자증권",
                "is_mock": False,
            },
            {
                "broker_type": "kis",
                "broker_name": "한국투자증권 (모의)",
                "is_mock": True,
            },
            {
                "broker_type": "upbit",
                "broker_name": "업비트",
                "is_mock": False,
            },
        ]

        created_count = 0
        for broker_data in default_brokers:
            # 이미 존재하는지 확인
            result = await db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == 1,
                    BrokerAccount.broker_type == broker_data["broker_type"],
                    BrokerAccount.is_mock == broker_data["is_mock"]
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                print(f"  - {broker_data['broker_name']}: already exists (ID: {existing.id})")
                continue

            # 생성
            broker = BrokerAccount(
                user_id=1,
                broker_type=broker_data["broker_type"],
                broker_name=broker_data["broker_name"],
                is_mock=broker_data["is_mock"],
                is_active=True
            )
            db.add(broker)
            created_count += 1
            print(f"  ✓ Created: {broker_data['broker_name']}")

        if created_count > 0:
            await db.commit()
            print(f"\n✓ Created {created_count} broker account(s)")
        else:
            print("\n✓ All broker accounts already exist")


if __name__ == "__main__":
    print("=" * 60)
    print("Creating default broker accounts...")
    print("=" * 60)
    asyncio.run(create_default_brokers())
    print("=" * 60)
    print("Done!")
