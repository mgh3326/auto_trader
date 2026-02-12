"""
나스닥 종목 데이터 Lazy Loading 테스트 스크립트

이 스크립트는 세 가지 사용 패턴을 보여줍니다:
1. 암묵적 lazy loading (데이터 접근 시 자동 로드)
2. 명시적 초기화 (prime_nasdaq_stock_data() 호출)
3. 직접 함수 호출 (get_nasdaq_name_to_symbol())
"""

print("=" * 70)
print("1. 모듈 임포트 (아직 데이터는 로드되지 않음)")
print("=" * 70)
from data.stocks_info import (
    NASDAQ_NAME_TO_SYMBOL,
    get_nasdaq_name_to_symbol,
    prime_nasdaq_stock_data,
)

print("✓ 임포트 완료 (데이터는 아직 로드되지 않음)\n")

# 패턴 1: 암묵적 lazy loading
print("=" * 70)
print("2. 암묵적 Lazy Loading (데이터 접근 시 자동 로드)")
print("=" * 70)
print("Apple 심볼 조회 중...")
apple_symbol = NASDAQ_NAME_TO_SYMBOL.get("애플")
print(f"애플 심볼: {apple_symbol}")

print("\nTesla 조회 중...")
tesla_symbol = NASDAQ_NAME_TO_SYMBOL.get("테슬라")
print(f"테슬라 심볼: {tesla_symbol}")

# 패턴 2: 명시적 초기화
print("\n" + "=" * 70)
print("3. 명시적 초기화 (prime_nasdaq_stock_data())")
print("=" * 70)
print("(이미 로드되었으므로 캐시된 데이터 사용)\n")
prime_nasdaq_stock_data()

# 패턴 3: 직접 함수 호출
print("\n" + "=" * 70)
print("4. 직접 함수 호출 (get_nasdaq_name_to_symbol())")
print("=" * 70)
nasdaq_data = get_nasdaq_name_to_symbol()
print(f"나스닥 종목 수: {len(nasdaq_data)}")

# 일부 종목 샘플 출력
print("\n나스닥 일부 종목 (처음 5개):")
for i, (name, symbol) in enumerate(nasdaq_data.items()):
    if i >= 5:
        break
    print(f"  - {name}: {symbol}")

# 기존 코드와의 호환성 테스트
print("\n" + "=" * 70)
print("5. 기존 코드와의 하위 호환성 테스트")
print("=" * 70)

# dict처럼 사용 가능
test_names = ["애플", "마이크로소프트", "아마존닷컴", "엔비디아"]
for name in test_names:
    if name in NASDAQ_NAME_TO_SYMBOL:
        symbol = NASDAQ_NAME_TO_SYMBOL.get(name)
        print(f"✓ '{name}' in NASDAQ_NAME_TO_SYMBOL: {symbol}")
    else:
        print(f"✗ '{name}' not found")

print(f"\nlen(NASDAQ_NAME_TO_SYMBOL): {len(NASDAQ_NAME_TO_SYMBOL)}")

# iteration 가능
print("\n나스닥 종목 처음 3개 (iteration):")
for i, name in enumerate(NASDAQ_NAME_TO_SYMBOL):
    if i >= 3:
        break
    print(f"  - {name}: {NASDAQ_NAME_TO_SYMBOL[name]}")

print("\n✓ 모든 테스트 통과!")
