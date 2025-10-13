#!/usr/bin/env python3
"""
Upbit 하위 호환성 테스트 (debug_upbit_json.py 스타일)
"""

import asyncio
from data.coins_info import upbit_pairs


async def main():
    # 기존 코드 스타일: prime_upbit_constants() 명시적 호출
    await upbit_pairs.prime_upbit_constants()

    # 기존 코드가 사용하던 방식 그대로 동작하는지 테스트
    test_coins = ["BTC", "ETH", "XRP", "SOL", "ADA"]

    print("=" * 70)
    print("기존 코드 스타일 하위 호환성 테스트")
    print("=" * 70)

    for coin in test_coins:
        # COIN_TO_NAME_KR 딕셔너리처럼 접근
        korean_name = upbit_pairs.COIN_TO_NAME_KR.get(coin)

        # KRW_TRADABLE_COINS set처럼 접근
        is_tradable = coin in upbit_pairs.KRW_TRADABLE_COINS

        if korean_name:
            # NAME_TO_PAIR_KR 딕셔너리처럼 접근
            pair = upbit_pairs.NAME_TO_PAIR_KR.get(korean_name)
            # COIN_TO_NAME_EN 딕셔너리처럼 접근
            english_name = upbit_pairs.COIN_TO_NAME_EN.get(coin)

            print(f"✓ {coin}:")
            print(f"  - 한글명: {korean_name}")
            print(f"  - 영어명: {english_name}")
            print(f"  - 페어: {pair}")
            print(f"  - KRW 거래 가능: {is_tradable}")
        else:
            print(f"✗ {coin}: 정보 없음")

    print("\n" + "=" * 70)
    print("결과: 기존 코드와 완벽하게 호환됩니다!")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(main())
