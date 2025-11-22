#!/usr/bin/env python3
"""
사용자 권한 관리 CLI 도구

사용법:
    python manage_users.py list                    # 모든 사용자 조회
    python manage_users.py promote <username>      # trader로 승격
    python manage_users.py admin <username>        # admin으로 승격
    python manage_users.py demote <username>       # viewer로 강등
    python manage_users.py activate <username>     # 사용자 활성화
    python manage_users.py deactivate <username>   # 사용자 비활성화
"""
import asyncio
import sys

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.trading import User, UserRole


async def list_users():
    """모든 사용자 조회"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).order_by(User.id))
            users = result.scalars().all()

            if not users:
                print("등록된 사용자가 없습니다.")
                return

            print("\n" + "=" * 80)
            print(f"{'ID':<5} {'사용자명':<20} {'이메일':<30} {'권한':<10} {'상태':<10}")
            print("=" * 80)

            for user in users:
                status = "활성" if user.is_active else "비활성"
                username = user.username or "N/A"
                email = user.email or "N/A"
                print(
                    f"{user.id:<5} {username:<20} {email:<30} "
                    f"{user.role.value:<10} {status:<10}"
                )

            print("=" * 80)
            print(f"총 {len(users)}명\n")
    except Exception as e:
        print(f"❌ 사용자 목록 조회 중 오류 발생: {e}")


async def change_role(username: str, new_role: UserRole):
    """사용자 권한 변경"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()

            if not user:
                print(f"❌ 사용자를 찾을 수 없습니다: {username}")
                return

            old_role = user.role.value
            user.role = new_role
            await db.commit()

            print(f"✅ {username}의 권한이 {old_role} → {new_role.value}(으)로 변경되었습니다.")
    except Exception as e:
        print(f"❌ 권한 변경 중 오류 발생: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


async def toggle_active(username: str, active: bool):
    """사용자 활성화/비활성화"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()

            if not user:
                print(f"❌ 사용자를 찾을 수 없습니다: {username}")
                return

            user.is_active = active
            await db.commit()

            status = "활성화" if active else "비활성화"
            print(f"✅ {username}이(가) {status}되었습니다.")
    except Exception as e:
        print(f"❌ 사용자 상태 변경 중 오류 발생: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


def print_usage():
    """사용법 출력"""
    print(__doc__)


async def main():
    """메인 함수"""
    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1]

    if command == "list":
        await list_users()

    elif command == "promote":
        if len(sys.argv) < 3:
            print("❌ 사용자명을 입력하세요: python manage_users.py promote <username>")
            return
        username = sys.argv[2]
        await change_role(username, UserRole.trader)

    elif command == "admin":
        if len(sys.argv) < 3:
            print("❌ 사용자명을 입력하세요: python manage_users.py admin <username>")
            return
        username = sys.argv[2]
        await change_role(username, UserRole.admin)

    elif command == "demote":
        if len(sys.argv) < 3:
            print("❌ 사용자명을 입력하세요: python manage_users.py demote <username>")
            return
        username = sys.argv[2]
        await change_role(username, UserRole.viewer)

    elif command == "activate":
        if len(sys.argv) < 3:
            print("❌ 사용자명을 입력하세요: python manage_users.py activate <username>")
            return
        username = sys.argv[2]
        await toggle_active(username, True)

    elif command == "deactivate":
        if len(sys.argv) < 3:
            print("❌ 사용자명을 입력하세요: python manage_users.py deactivate <username>")
            return
        username = sys.argv[2]
        await toggle_active(username, False)

    else:
        print(f"❌ 알 수 없는 명령어: {command}")
        print_usage()


if __name__ == "__main__":
    asyncio.run(main())
