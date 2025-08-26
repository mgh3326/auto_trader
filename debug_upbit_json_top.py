#!/usr/bin/env python3
"""
Upbit JSON 분석 실행 예시
"""

import asyncio
from app.analysis.service_analyzers import UpbitAnalyzer
from app.services import upbit
from data.coins_info import upbit_pairs


async def main():
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()

    # JSON 분석기 초기화
    analyzer = UpbitAnalyzer()

    try:
        # 보유 코인 정보 가져오기
        traded_coins = await upbit.fetch_top_traded_coins()
        # 보유 코인의 한국 이름으로 coin_names 생성
        coin_names = []
        for coin in traded_coins[:10]:
            coin_name = coin.get("market")
            # COIN_TO_NAME_KR에서 한국 이름 찾기
            korean_name = upbit_pairs.PAIR_TO_NAME_KR.get(coin_name)
            if korean_name:
                coin_names.append(korean_name)
        if not coin_names:
            print("분석 가능한 코인이 없습니다.")
            return

        print(f"\n분석할 코인 목록: {coin_names}")
        # JSON 형식으로 코인 분석 실행
        await analyzer.analyze_coins_json(coin_names)

    except Exception as e:
        print(f"에러 발생: {e}")
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
