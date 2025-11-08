#!/usr/bin/env python3
"""
에러 리포팅 테스트 스크립트

이 스크립트는 다음을 테스트합니다:
1. ErrorReporter 연결 테스트
2. 단순 에러 리포팅
3. 컨텍스트 정보가 포함된 에러 리포팅
4. 중복 에러 방지 (Redis 기반)
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from redis.asyncio import Redis

from app.monitoring.error_reporter import get_error_reporter

# .env 파일 로드
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)


def check_environment():
    """환경 변수 확인"""
    required_vars = {
        "ERROR_REPORTING_ENABLED": os.getenv("ERROR_REPORTING_ENABLED"),
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
        "ERROR_REPORTING_CHAT_ID": os.getenv("ERROR_REPORTING_CHAT_ID"),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
        "REDIS_URL": os.getenv("REDIS_URL"),
    }

    print("\n" + "=" * 60)
    print("📋 환경 변수 확인")
    print("=" * 60)

    # Check ERROR_REPORTING_CHAT_ID or fallback to TELEGRAM_CHAT_ID
    error_chat_id = os.getenv("ERROR_REPORTING_CHAT_ID")
    fallback_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    chat_id = error_chat_id or fallback_chat_id

    for var_name, var_value in required_vars.items():
        status = "✅ 설정됨" if var_value else "❌ 없음"
        print(f"{var_name}: {status}")

    # Check if we have a chat ID (either ERROR_REPORTING_CHAT_ID or TELEGRAM_CHAT_ID)
    if not chat_id:
        print("\n⚠️  ERROR_REPORTING_CHAT_ID 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다!")
        print("\n💡 .env 파일에서 다음 중 하나를 설정하세요:")
        print("   ERROR_REPORTING_CHAT_ID=your_chat_id  (권장)")
        print("   또는")
        print("   TELEGRAM_CHAT_ID=your_chat_id  (fallback)")
        return False, None

    if error_chat_id:
        print(f"\n✅ 에러 리포팅 Chat ID 사용: ERROR_REPORTING_CHAT_ID")
    else:
        print(f"\n💡 Fallback Chat ID 사용: TELEGRAM_CHAT_ID")

    # Check other required vars
    if not os.getenv("TELEGRAM_TOKEN"):
        print("\n⚠️  TELEGRAM_TOKEN이 설정되지 않았습니다!")
        print("   .env 파일에 TELEGRAM_TOKEN=your_token을 설정하세요.")
        return False, None

    if not os.getenv("REDIS_URL"):
        print("\n⚠️  REDIS_URL이 설정되지 않았습니다!")
        print("   .env 파일에 REDIS_URL=redis://localhost:6379/0을 설정하세요.")
        return False, None

    # Check if error reporting is enabled
    if os.getenv("ERROR_REPORTING_ENABLED", "").lower() != "true":
        print("\n⚠️  ERROR_REPORTING_ENABLED가 'true'가 아닙니다.")
        print("   에러 리포팅이 비활성화되어 있습니다.")
        return False, None

    return True, chat_id


async def test_connection(error_reporter):
    """연결 테스트"""
    print("\n" + "=" * 60)
    print("1️⃣  테스트 1: Telegram 연결 테스트")
    print("=" * 60)

    result = await error_reporter.test_connection()
    if result:
        print("✅ 연결 성공! 텔레그램 메시지를 확인하세요.")
    else:
        print("❌ 연결 실패!")
    return result


async def test_simple_error(error_reporter):
    """단순 에러 테스트"""
    print("\n" + "=" * 60)
    print("2️⃣  테스트 2: 단순 에러 리포팅")
    print("=" * 60)

    try:
        # Intentionally raise an error
        result = 10 / 0
    except ZeroDivisionError as e:
        print("에러 발생: ZeroDivisionError")
        result = await error_reporter.send_error_to_telegram(e)
        if result:
            print("✅ 에러 리포팅 성공! 텔레그램 메시지를 확인하세요.")
        else:
            print("❌ 에러 리포팅 실패!")
        return result


async def test_error_with_context(error_reporter):
    """컨텍스트 정보가 포함된 에러 테스트"""
    print("\n" + "=" * 60)
    print("3️⃣  테스트 3: 컨텍스트 정보가 포함된 에러")
    print("=" * 60)

    try:
        # Simulate an API error
        data = {"user_id": 12345, "action": "buy", "symbol": "BTC"}
        price = data["price"]  # KeyError
    except KeyError as e:
        print("에러 발생: KeyError")
        result = await error_reporter.send_error_to_telegram(
            e,
            additional_context={
                "user_id": 12345,
                "action": "buy",
                "symbol": "BTC",
                "missing_key": "price",
            },
        )
        if result:
            print("✅ 컨텍스트 정보가 포함된 에러 리포팅 성공!")
        else:
            print("❌ 에러 리포팅 실패!")
        return result


async def test_duplicate_prevention(error_reporter):
    """중복 에러 방지 테스트"""
    print("\n" + "=" * 60)
    print("4️⃣  테스트 4: 중복 에러 방지 (Redis 기반)")
    print("=" * 60)

    try:
        # Raise the same error multiple times
        for i in range(3):
            print(f"\n   시도 {i + 1}/3:")
            try:
                items = [1, 2, 3]
                value = items[10]  # IndexError
            except IndexError as e:
                result = await error_reporter.send_error_to_telegram(e)
                if result:
                    print(f"   ✅ 에러 리포팅 전송됨")
                else:
                    print(f"   ⏭️  중복 에러로 스킵됨 (예상된 동작)")

            # Wait a bit between attempts
            if i < 2:
                await asyncio.sleep(1)

        print("\n💡 첫 번째 시도만 텔레그램에 전송되어야 합니다.")
        print("   나머지는 중복 방지 기능으로 스킵됩니다.")
        return True

    except Exception as e:
        print(f"❌ 테스트 실패: {e}")
        return False


async def test_complex_error(error_reporter):
    """복잡한 에러 테스트 (긴 스택 트레이스)"""
    print("\n" + "=" * 60)
    print("5️⃣  테스트 5: 복잡한 에러 (긴 스택 트레이스)")
    print("=" * 60)

    def level_3():
        return {"data": None}["data"]["nested"]["value"]

    def level_2():
        return level_3()

    def level_1():
        return level_2()

    try:
        level_1()
    except (TypeError, KeyError) as e:
        print("에러 발생: 중첩된 함수 호출에서 발생한 에러")
        result = await error_reporter.send_error_to_telegram(
            e,
            additional_context={
                "component": "data_processor",
                "operation": "nested_access",
            },
        )
        if result:
            print("✅ 복잡한 에러 리포팅 성공!")
        else:
            print("❌ 에러 리포팅 실패!")
        return result


async def main():
    """메인 테스트 함수"""
    print("\n" + "=" * 60)
    print("🧪 에러 리포팅 시스템 테스트")
    print("=" * 60)

    # 1. Check environment variables
    env_ok, chat_id = check_environment()
    if not env_ok:
        print("\n❌ 환경 설정이 완료되지 않았습니다.")
        sys.exit(1)

    # 2. Setup Redis and ErrorReporter
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client = None
    error_reporter = None

    try:
        # Connect to Redis
        print("\n" + "=" * 60)
        print("🔌 Redis 연결 중...")
        print("=" * 60)
        redis_client = Redis.from_url(redis_url, decode_responses=True)

        # Test Redis connection
        await redis_client.ping()
        print("✅ Redis 연결 성공")

        # Configure error reporter
        error_reporter = get_error_reporter()
        error_reporter.configure(
            bot_token=os.getenv("TELEGRAM_TOKEN"),
            chat_id=chat_id,
            redis_client=redis_client,
            enabled=True,
            duplicate_window=int(os.getenv("ERROR_DUPLICATE_WINDOW", "300")),
        )
        print(f"✅ ErrorReporter 설정 완료 (chat_id: {chat_id})")

        # Run tests
        test_results = []

        # Test 1: Connection
        result = await test_connection(error_reporter)
        test_results.append(("연결 테스트", result))
        await asyncio.sleep(2)

        # Test 2: Simple error
        result = await test_simple_error(error_reporter)
        test_results.append(("단순 에러", result))
        await asyncio.sleep(2)

        # Test 3: Error with context
        result = await test_error_with_context(error_reporter)
        test_results.append(("컨텍스트 에러", result))
        await asyncio.sleep(2)

        # Test 4: Duplicate prevention
        result = await test_duplicate_prevention(error_reporter)
        test_results.append(("중복 방지", result))
        await asyncio.sleep(2)

        # Test 5: Complex error
        result = await test_complex_error(error_reporter)
        test_results.append(("복잡한 에러", result))

        # Summary
        print("\n" + "=" * 60)
        print("📊 테스트 결과 요약")
        print("=" * 60)

        passed = sum(1 for _, result in test_results if result)
        total = len(test_results)

        for test_name, result in test_results:
            status = "✅ 통과" if result else "❌ 실패"
            print(f"{test_name}: {status}")

        print("\n" + "=" * 60)
        print(f"총 {passed}/{total}개 테스트 통과")
        print("=" * 60)

        if passed == total:
            print("\n🎉 모든 테스트가 성공적으로 완료되었습니다!")
            print("\n📱 텔레그램에서 다음 메시지들을 확인하세요:")
            print("   1. ✅ Telegram Error Reporter Test (연결 테스트)")
            print("   2. 🚨 ZeroDivisionError (단순 에러)")
            print("   3. 🚨 KeyError (컨텍스트 에러)")
            print("   4. 🚨 IndexError (중복 방지 - 1개만 전송됨)")
            print("   5. 🚨 TypeError/KeyError (복잡한 에러)")
        else:
            print("\n⚠️  일부 테스트가 실패했습니다.")

    except Exception as e:
        print(f"\n❌ 테스트 실행 중 오류 발생: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    finally:
        # Cleanup
        if error_reporter:
            await error_reporter.shutdown()
            print("\n🧹 ErrorReporter 정리 완료")

        if redis_client:
            await redis_client.aclose()
            print("🧹 Redis 연결 종료")


if __name__ == "__main__":
    asyncio.run(main())
