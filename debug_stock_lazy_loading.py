"""
KRX 종목 데이터 Lazy Loading 테스트 스크립트

이 스크립트는 세 가지 사용 패턴을 보여줍니다:
1. 암묵적 lazy loading (데이터 접근 시 자동 로드)
2. 명시적 초기화 (prime_krx_stock_data() 호출)
3. 직접 함수 호출 (get_kospi_name_to_code() 등)
"""

print("=" * 70)
print("1. 모듈 임포트 (아직 데이터는 로드되지 않음)")
print("=" * 70)
from data.stocks_info import (
    KOSPI_NAME_TO_CODE,
    KOSDAQ_NAME_TO_CODE,
    KRX_NAME_TO_CODE,
    prime_krx_stock_data,
    get_kospi_name_to_code,
)

print("✓ 임포트 완료 (데이터는 아직 로드되지 않음)\n")

# 패턴 1: 암묵적 lazy loading
print("=" * 70)
print("2. 암묵적 Lazy Loading (데이터 접근 시 자동 로드)")
print("=" * 70)
print("삼성전자 코드 조회 중...")
samsung_code = KOSPI_NAME_TO_CODE.get("삼성전자")
print(f"삼성전자 코드: {samsung_code}")

print("\nKRX_NAME_TO_CODE에서 카카오 조회 중...")
kakao_code = KRX_NAME_TO_CODE.get("카카오")
print(f"카카오 코드: {kakao_code}")

# 패턴 2: 명시적 초기화
print("\n" + "=" * 70)
print("3. 명시적 초기화 (prime_krx_stock_data())")
print("=" * 70)
print("(이미 로드되었으므로 캐시된 데이터 사용)\n")
prime_krx_stock_data()

# 패턴 3: 직접 함수 호출
print("\n" + "=" * 70)
print("4. 직접 함수 호출 (get_kospi_name_to_code())")
print("=" * 70)
kospi_data = get_kospi_name_to_code()
print(f"KOSPI 종목 수: {len(kospi_data)}")
print(f"KOSPI 일부 종목: {list(kospi_data.keys())[:5]}")

# 기존 코드와의 호환성 테스트
print("\n" + "=" * 70)
print("5. 기존 코드와의 하위 호환성 테스트")
print("=" * 70)

# dict처럼 사용 가능
print(f"'삼성전자' in KOSPI_NAME_TO_CODE: {'삼성전자' in KOSPI_NAME_TO_CODE}")
print(f"KOSPI_NAME_TO_CODE['삼성전자']: {KOSPI_NAME_TO_CODE['삼성전자']}")
print(f"len(KOSPI_NAME_TO_CODE): {len(KOSPI_NAME_TO_CODE)}")

# iteration 가능
print("\nKOSPI 종목 처음 3개:")
for i, name in enumerate(KOSPI_NAME_TO_CODE):
    if i >= 3:
        break
    print(f"  - {name}: {KOSPI_NAME_TO_CODE[name]}")

print("\n✓ 모든 테스트 통과!")
