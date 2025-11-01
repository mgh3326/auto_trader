"""기본 사용자 생성 스크립트"""
import asyncio
from app.core.db import AsyncSessionLocal
from app.models.trading import User
from sqlalchemy import select


async def create_default_user():
    """기본 사용자 생성"""
    async with AsyncSessionLocal() as db:
        # 이미 user_id=1이 있는지 확인
        result = await db.execute(select(User).where(User.id == 1))
        existing_user = result.scalar_one_or_none()

        if existing_user:
            print(f"✓ User ID 1 already exists: {existing_user.email or existing_user.nickname or 'No name'}")
            return

        # 기본 사용자 생성
        default_user = User(
            email="default@auto-trader.local",
            nickname="기본사용자",
            tz="Asia/Seoul",
            base_currency="KRW"
        )

        db.add(default_user)
        await db.commit()
        await db.refresh(default_user)

        print(f"✓ Default user created successfully!")
        print(f"  - ID: {default_user.id}")
        print(f"  - Email: {default_user.email}")
        print(f"  - Nickname: {default_user.nickname}")
        print(f"  - Timezone: {default_user.tz}")
        print(f"  - Base Currency: {default_user.base_currency}")


if __name__ == "__main__":
    print("=" * 60)
    print("Creating default user...")
    print("=" * 60)
    asyncio.run(create_default_user())
    print("=" * 60)
    print("Done!")
