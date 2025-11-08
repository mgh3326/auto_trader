#!/usr/bin/env python3
"""
텔레그램 알림 테스트 스크립트
"""
import asyncio
import sys
import os
from pathlib import Path
from dotenv import load_dotenv
from app.monitoring.trade_notifier import get_trade_notifier

# .env 파일 로드
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)


async def test_notifications():
    """텔레그램 알림 테스트"""
    # 환경 변수에서 설정 로드
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    print(f"DEBUG: TELEGRAM_TOKEN={'설정됨' if token else '없음'}")
    print(f"DEBUG: TELEGRAM_CHAT_ID={'설정됨' if chat_id else '없음'}")

    if not token or not chat_id:
        print("❌ 텔레그램 설정이 없습니다!")
        print("   .env 파일에 다음 변수를 설정하세요:")
        print("   - TELEGRAM_TOKEN")
        print("   - TELEGRAM_CHAT_ID")
        return

    chat_ids = [chat_id.strip()]

    # TradeNotifier 초기화
    notifier = get_trade_notifier()
    notifier.configure(
        bot_token=token,
        chat_ids=chat_ids,
        enabled=True
    )

    try:
        print("🧪 텔레그램 알림 테스트 시작...")
        print("=" * 60)

        # 테스트 1: 연결 테스트
        print("\n1️⃣ 테스트 1: 연결 테스트")
        result = await notifier.test_connection()
        if result:
            print("   ✅ 연결 성공! 텔레그램 메시지를 확인하세요.")
        else:
            print("   ❌ 연결 실패!")
            return

        await asyncio.sleep(2)

        # 테스트 2: AI 분석 알림
        print("\n2️⃣ 테스트 2: AI 분석 완료 알림")
        result = await notifier.notify_analysis_complete(
            symbol="BTC",
            korean_name="비트코인",
            decision="buy",
            confidence=85.5,
            reasons=[
                "상승 추세 지속 중",
                "거래량 증가",
                "지지선 강화"
            ],
            market_type="암호화폐"
        )
        print(f"   {'✅' if result else '❌'} AI 분석 알림 전송")

        await asyncio.sleep(2)

        # 테스트 3: 매수 알림
        print("\n3️⃣ 테스트 3: 매수 주문 알림")
        result = await notifier.notify_buy_order(
            symbol="BTC",
            korean_name="비트코인",
            order_count=3,
            total_amount=100000,
            prices=[50000000, 49500000, 49000000],
            volumes=[0.001, 0.001, 0.001],
            market_type="암호화폐"
        )
        print(f"   {'✅' if result else '❌'} 매수 알림 전송")

        await asyncio.sleep(2)

        # 테스트 4: 매도 알림
        print("\n4️⃣ 테스트 4: 매도 주문 알림")
        result = await notifier.notify_sell_order(
            symbol="BTC",
            korean_name="비트코인",
            order_count=2,
            total_volume=0.002,
            prices=[51000000, 51500000],
            volumes=[0.001, 0.001],
            expected_amount=102000,
            market_type="암호화폐"
        )
        print(f"   {'✅' if result else '❌'} 매도 알림 전송")

        await asyncio.sleep(2)

        # 테스트 5: 주문 취소 알림
        print("\n5️⃣ 테스트 5: 주문 취소 알림")
        result = await notifier.notify_cancel_orders(
            symbol="ETH",
            korean_name="이더리움",
            cancel_count=5,
            order_type="매수",
            market_type="암호화폐"
        )
        print(f"   {'✅' if result else '❌'} 취소 알림 전송")

        await asyncio.sleep(2)

        # 테스트 6: 자동화 요약 알림
        print("\n6️⃣ 테스트 6: 자동화 실행 요약")
        result = await notifier.notify_automation_summary(
            total_coins=50,
            analyzed=45,
            bought=3,
            sold=2,
            errors=1,
            duration_seconds=125.5
        )
        print(f"   {'✅' if result else '❌'} 요약 알림 전송")

        print("\n" + "=" * 60)
        print("✅ 모든 테스트 완료!")
        print("\n📱 텔레그램에서 다음 메시지들을 확인하세요:")
        print("   1. ✅ 거래 알림 테스트 (연결 확인)")
        print("   2. 🟢 AI 분석 완료 - 비트코인 매수")
        print("   3. 💰 매수 주문 체결 - 비트코인")
        print("   4. 💸 매도 주문 체결 - 비트코인")
        print("   5. 🚫 주문 취소 - 이더리움")
        print("   6. 🤖 자동 거래 실행 완료")

    finally:
        await notifier.shutdown()


async def main():
    """메인 실행 함수"""
    print("\n" + "=" * 60)
    print("📱 텔레그램 알림 테스트")
    print("=" * 60)

    await test_notifications()


if __name__ == "__main__":
    asyncio.run(main())
