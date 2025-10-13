#!/usr/bin/env python3
"""
KIS 해외주식 API 테스트
각 API 엔드포인트를 개별적으로 테스트하여 어느 API가 작동하는지 확인
"""

import asyncio
from app.services.kis import kis


async def test_overseas_price():
    """해외주식 현재가 조회 테스트"""
    print("=== 해외주식 현재가 조회 테스트 ===")
    try:
        df = await kis.inquire_overseas_price("AAPL", "NASD")
        print(f"✓ 현재가 조회 성공:")
        print(df)
        return True
    except Exception as e:
        print(f"✗ 현재가 조회 실패: {e}")
        return False


async def test_overseas_fundamental():
    """해외주식 기본 정보 조회 테스트"""
    print("\n=== 해외주식 기본 정보 조회 테스트 ===")
    try:
        info = await kis.fetch_overseas_fundamental_info("AAPL", "NASD")
        print(f"✓ 기본 정보 조회 성공:")
        print(info)
        return True
    except Exception as e:
        print(f"✗ 기본 정보 조회 실패: {e}")
        return False


async def test_overseas_daily():
    """해외주식 일봉 조회 테스트"""
    print("\n=== 해외주식 일봉 조회 테스트 ===")
    try:
        df = await kis.inquire_overseas_daily_price("AAPL", "NASD", n=10)
        print(f"✓ 일봉 조회 성공: {len(df)}개 데이터")
        print(df.head())
        return True
    except Exception as e:
        print(f"✗ 일봉 조회 실패: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_overseas_minute():
    """해외주식 분봉 조회 테스트"""
    print("\n=== 해외주식 분봉 조회 테스트 ===")
    try:
        df = await kis.inquire_overseas_minute_chart("AAPL", "NASD", n=10)
        print(f"✓ 분봉 조회 성공: {len(df)}개 데이터")
        print(df.head())
        return True
    except Exception as e:
        print(f"✗ 분봉 조회 실패: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    print("KIS 해외주식 API 테스트 시작\n")
    print("=" * 70)

    results = {}

    # 각 API 테스트
    results["현재가"] = await test_overseas_price()
    results["기본정보"] = await test_overseas_fundamental()
    results["일봉"] = await test_overseas_daily()
    results["분봉"] = await test_overseas_minute()

    # 결과 요약
    print("\n" + "=" * 70)
    print("테스트 결과 요약:")
    print("=" * 70)
    for api_name, success in results.items():
        status = "✓ 작동" if success else "✗ 실패"
        print(f"{api_name}: {status}")

    print("\n모든 테스트 완료")


if __name__ == "__main__":
    asyncio.run(main())
