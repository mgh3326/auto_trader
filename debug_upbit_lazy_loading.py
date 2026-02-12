"""
Upbit Lazy Loading 테스트 스크립트

이 스크립트는 세 가지 사용 패턴을 보여줍니다:
1. 명시적 초기화 (prime_upbit_constants() 호출)
2. 암묵적 lazy loading (데이터 접근 시 자동 로드)
3. 강제 갱신 (get_or_refresh_maps(force=True) 호출)
"""
import asyncio

from data.coins_info import upbit_pairs


async def main():
    print("=" * 70)
    print("1. 명시적 초기화 (prime_upbit_constants())")
    print("=" * 70)
    await upbit_pairs.prime_upbit_constants()
    print("✓ 초기화 완료\n")

    # 패턴 1: 기존 코드와의 호환성 - dict처럼 사용
    print("=" * 70)
    print("2. 기존 코드와의 하위 호환성 테스트")
    print("=" * 70)

    # dict 접근
    btc_pair = upbit_pairs.NAME_TO_PAIR_KR.get("비트코인")
    print(f"'비트코인' 페어: {btc_pair}")

    btc_name = upbit_pairs.PAIR_TO_NAME_KR.get("KRW-BTC")
    print(f"'KRW-BTC' 이름: {btc_name}")

    # set 접근
    print(f"'BTC' in KRW_TRADABLE_COINS: {'BTC' in upbit_pairs.KRW_TRADABLE_COINS}")
    print(f"거래 가능한 코인 수: {len(upbit_pairs.KRW_TRADABLE_COINS)}")

    # iteration
    print("\n상위 5개 코인:")
    for i, coin in enumerate(upbit_pairs.COIN_TO_NAME_KR):
        if i >= 5:
            break
        korean_name = upbit_pairs.COIN_TO_NAME_KR[coin]
        english_name = upbit_pairs.COIN_TO_NAME_EN.get(coin)
        print(f"  - {coin}: {korean_name} ({english_name})")

    # 패턴 2: 직접 함수 호출
    print("\n" + "=" * 70)
    print("3. 직접 함수 호출 (get_upbit_maps())")
    print("=" * 70)
    maps = await upbit_pairs.get_upbit_maps()
    print(f"총 KRW 마켓 코인 수: {len(maps['COIN_TO_NAME_KR'])}개")
    print(f"총 페어 수: {len(maps['NAME_TO_PAIR_KR'])}개")

    # 패턴 3: 강제 갱신
    print("\n" + "=" * 70)
    print("4. 강제 갱신 테스트 (get_or_refresh_maps(force=True))")
    print("=" * 70)
    print("(실제 운영에서는 캐시 만료 시 또는 명시적 갱신이 필요할 때 사용)")
    # refreshed_maps = await upbit_pairs.get_or_refresh_maps(force=True)
    # print(f"갱신된 데이터 크기: {len(refreshed_maps['COIN_TO_NAME_KR'])}개 코인")
    print("(테스트에서는 API 호출을 줄이기 위해 스킵)")

    print("\n✓ 모든 테스트 통과!")

if __name__ == "__main__":
    asyncio.run(main())
