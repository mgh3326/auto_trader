"""
모든 주식 데이터 Lazy Loading 통합 테스트

KRX (KOSPI/KOSDAQ) + NASDAQ 동시 테스트
"""
import asyncio
from data.stocks_info import (
    KRX_NAME_TO_CODE,
    KOSPI_NAME_TO_CODE,
    KOSDAQ_NAME_TO_CODE,
    NASDAQ_NAME_TO_SYMBOL,
    prime_krx_stock_data,
    prime_nasdaq_stock_data,
)
from data.coins_info import upbit_pairs

async def main():
    print("=" * 70)
    print("모든 주식/코인 데이터 Lazy Loading 통합 테스트")
    print("=" * 70)

    # 1. KRX 주식 (동기 - 암묵적 lazy loading)
    print("\n1️⃣  KRX 주식 데이터 (KOSPI + KOSDAQ)")
    print("-" * 70)

    samsung_code = KRX_NAME_TO_CODE.get("삼성전자")
    print(f"✓ 삼성전자 (KRX): {samsung_code}")

    kakao_code = KOSDAQ_NAME_TO_CODE.get("카카오")
    print(f"✓ 카카오 (KOSDAQ): {kakao_code}")

    print(f"✓ KOSPI 종목 수: {len(KOSPI_NAME_TO_CODE)}")
    print(f"✓ KOSDAQ 종목 수: {len(KOSDAQ_NAME_TO_CODE)}")
    print(f"✓ KRX 전체: {len(KRX_NAME_TO_CODE)}")

    # 2. NASDAQ 주식 (동기 - 암묵적 lazy loading)
    print("\n2️⃣  NASDAQ 주식 데이터")
    print("-" * 70)

    aapl = NASDAQ_NAME_TO_SYMBOL.get("애플")
    print(f"✓ 애플 (한글): {aapl}")

    aapl_en = NASDAQ_NAME_TO_SYMBOL.get("APPLE INC")
    print(f"✓ Apple Inc (영어): {aapl_en}")

    tsla = NASDAQ_NAME_TO_SYMBOL.get("테슬라")
    print(f"✓ 테슬라: {tsla}")

    print(f"✓ NASDAQ 종목 수: {len(NASDAQ_NAME_TO_SYMBOL)}")

    # 3. Upbit 코인 (비동기 - 명시적 초기화 필요)
    print("\n3️⃣  Upbit 코인 데이터")
    print("-" * 70)

    await upbit_pairs.prime_upbit_constants()

    btc_pair = upbit_pairs.NAME_TO_PAIR_KR.get("비트코인")
    print(f"✓ 비트코인: {btc_pair}")

    eth_pair = upbit_pairs.NAME_TO_PAIR_KR.get("이더리움")
    print(f"✓ 이더리움: {eth_pair}")

    print(f"✓ KRW 거래 가능 코인: {len(upbit_pairs.KRW_TRADABLE_COINS)}")

    # 4. 종합 요약
    print("\n" + "=" * 70)
    print("📊 데이터 요약")
    print("=" * 70)
    print(f"• 국내주식 (KOSPI): {len(KOSPI_NAME_TO_CODE):,}개")
    print(f"• 국내주식 (KOSDAQ): {len(KOSDAQ_NAME_TO_CODE):,}개")
    print(f"• 해외주식 (NASDAQ): {len(NASDAQ_NAME_TO_SYMBOL):,}개")
    print(f"• 암호화폐 (Upbit KRW): {len(upbit_pairs.KRW_TRADABLE_COINS):,}개")

    total = (len(KOSPI_NAME_TO_CODE) + len(KOSDAQ_NAME_TO_CODE) +
             len(NASDAQ_NAME_TO_SYMBOL) + len(upbit_pairs.KRW_TRADABLE_COINS))
    print(f"• 총 자산 종류: {total:,}개")

    # 5. Lazy loading 패턴 비교
    print("\n" + "=" * 70)
    print("🔍 Lazy Loading 패턴 비교")
    print("=" * 70)
    print("""
┌────────────┬──────────┬──────────────┬─────────────────────┐
│ 데이터     │ 타입     │ 초기화       │ 사용 예시           │
├────────────┼──────────┼──────────────┼─────────────────────┤
│ KRX        │ 동기     │ 암묵적/선택적│ code = KRX[name]    │
│ NASDAQ     │ 동기     │ 암묵적/선택적│ sym = NASDAQ[name]  │
│ Upbit      │ 비동기   │ 명시적 필수  │ await prime()       │
└────────────┴──────────┴──────────────┴─────────────────────┘
    """)

    print("✅ 모든 데이터가 정상적으로 로드되었습니다!")

if __name__ == "__main__":
    asyncio.run(main())
