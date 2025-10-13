"""
Upbit Lazy Loading 테스트 - prime 없이

이 스크립트는 prime_upbit_constants()를 호출하지 않고
데이터 접근 시 에러가 발생함을 보여줍니다.

비동기 환경에서 lazy loading은 명시적 초기화가 필요합니다.
"""
import asyncio
from data.coins_info import upbit_pairs

async def test_without_prime():
    """prime 없이 접근 시 에러 발생 테스트"""
    print("=" * 70)
    print("prime_upbit_constants() 호출 없이 데이터 접근 테스트")
    print("=" * 70)

    try:
        # 이 시점에서 _upbit_maps는 None이므로 RuntimeError 발생
        btc_pair = upbit_pairs.NAME_TO_PAIR_KR.get("비트코인")
        print(f"비트코인 페어: {btc_pair}")
    except RuntimeError as e:
        print(f"✓ 예상된 에러 발생: {e}\n")

async def test_with_prime():
    """prime 호출 후 정상 동작 테스트"""
    print("=" * 70)
    print("prime_upbit_constants() 호출 후 데이터 접근")
    print("=" * 70)

    # 명시적 초기화
    await upbit_pairs.prime_upbit_constants()
    print("✓ 초기화 완료\n")

    # 이제 정상 동작
    btc_pair = upbit_pairs.NAME_TO_PAIR_KR.get("비트코인")
    print(f"✓ 비트코인 페어: {btc_pair}")
    print(f"✓ 거래 가능한 코인 수: {len(upbit_pairs.KRW_TRADABLE_COINS)}개")

async def test_get_maps():
    """get_upbit_maps()로 직접 초기화 후 사용"""
    print("\n" + "=" * 70)
    print("get_upbit_maps()로 직접 초기화")
    print("=" * 70)

    # 새로운 프로세스라고 가정 (데이터 초기화)
    import importlib
    importlib.reload(upbit_pairs)

    # get_upbit_maps()로 초기화
    maps = await upbit_pairs.get_upbit_maps()
    print(f"✓ 초기화 완료: {len(maps['COIN_TO_NAME_KR'])}개 코인\n")

    # 이제 래퍼로도 접근 가능
    btc_pair = upbit_pairs.NAME_TO_PAIR_KR.get("비트코인")
    print(f"✓ 비트코인 페어: {btc_pair}")

async def main():
    print("\n=== Upbit 비동기 Lazy Loading 패턴 데모 ===\n")

    # 1. prime 없이 접근 시 에러
    await test_without_prime()

    # 2. prime 후 정상 동작
    await test_with_prime()

    # 3. get_upbit_maps()로 직접 초기화
    await test_get_maps()

    print("\n" + "=" * 70)
    print("결론:")
    print("=" * 70)
    print("비동기 환경에서는 데이터 접근 전에 반드시")
    print("- await prime_upbit_constants() 또는")
    print("- await get_upbit_maps()")
    print("를 호출해야 합니다.")
    print("\n✓ 모든 테스트 완료!")

if __name__ == "__main__":
    asyncio.run(main())
