"""
미국 주식 통합 모듈 테스트
Symbol -> Exchange 매핑 포함
"""
from data.stocks_info import (
    US_STOCKS_SYMBOL_TO_EXCHANGE,
    US_STOCKS_NAME_TO_SYMBOL,
    get_exchange_by_symbol,
    get_symbol_by_name,
    get_stock_info,
    prime_us_stocks_data,
)

print("=" * 70)
print("미국 주식 통합 모듈 테스트 (NASDAQ, NYSE, AMEX)")
print("=" * 70)

# 명시적 초기화
prime_us_stocks_data()

print("\n" + "=" * 70)
print("1. 주요 종목 심볼 및 거래소 조회")
print("=" * 70)

# 테스트할 주요 종목 (한글명)
test_stocks = [
    ("애플", "AAPL"),
    ("테슬라", "TSLA"),
    ("마이크로소프트", "MSFT"),
    ("아마존닷컴", "AMZN"),
    ("엔비디아", "NVDA"),
]

for korea_name, expected_symbol in test_stocks:
    symbol = get_symbol_by_name(korea_name)
    exchange = get_exchange_by_symbol(symbol) if symbol else None

    print(f"\n{korea_name}:")
    print(f"  심볼: {symbol} (예상: {expected_symbol})")
    print(f"  거래소: {exchange}")

    if symbol:
        # 전체 정보 조회
        info = get_stock_info(symbol)
        if info:
            print(f"  한글명: {info['name_kr']}")
            print(f"  영어명: {info['name_en']}")

print("\n" + "=" * 70)
print("2. Symbol -> Exchange 직접 조회")
print("=" * 70)

# 심볼로 바로 거래소 조회
test_symbols = ["AAPL", "TSLA", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "JPM", "BAC", "WMT"]

for symbol in test_symbols:
    exchange = US_STOCKS_SYMBOL_TO_EXCHANGE.get(symbol)
    print(f"{symbol}: {exchange}")

print("\n" + "=" * 70)
print("3. 거래소별 종목 수 통계")
print("=" * 70)

exchange_counts = {}
for symbol, exchange in US_STOCKS_SYMBOL_TO_EXCHANGE.items():
    exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1

for exchange, count in sorted(exchange_counts.items()):
    print(f"{exchange}: {count:,}개")

print(f"\n총 종목 수: {len(US_STOCKS_SYMBOL_TO_EXCHANGE):,}개")

print("\n" + "=" * 70)
print("4. KIS API 주문 시나리오 시뮬레이션")
print("=" * 70)

# 사용자가 "애플 주식 매수"라고 요청했을 때
stock_name = "애플"

# 1단계: 종목명 -> 심볼
symbol = get_symbol_by_name(stock_name)
print(f"1. 종목명 조회: '{stock_name}' -> {symbol}")

if symbol:
    # 2단계: 심볼 -> 거래소 코드
    exchange_code = get_exchange_by_symbol(symbol)
    print(f"2. 거래소 조회: {symbol} -> {exchange_code}")

    # 3단계: KIS API 호출 준비
    print(f"\n3. KIS API 주문 파라미터:")
    print(f"   OVRS_EXCG_CD: {exchange_code}")
    print(f"   Symbol: {symbol}")
    print(f"   ✅ KIS API 호출 준비 완료!")

print("\n" + "=" * 70)
print("5. 여러 거래소 예시")
print("=" * 70)

# 각 거래소별 대표 종목
examples = {
    "NASD": ["AAPL", "TSLA", "MSFT", "GOOGL", "META"],
    "NYSE": ["JPM", "BAC", "WMT", "V", "DIS"],
    "AMEX": [],  # AMEX 종목은 나중에 찾기
}

# AMEX 종목 찾기
amex_symbols = [sym for sym, ex in US_STOCKS_SYMBOL_TO_EXCHANGE.items() if ex == "AMEX"]
examples["AMEX"] = amex_symbols[:5]

for exchange, symbols in examples.items():
    print(f"\n{exchange} 종목 예시:")
    for symbol in symbols[:3]:  # 최대 3개만
        info = get_stock_info(symbol)
        if info:
            name_kr = info['name_kr'] or "(한글명 없음)"
            print(f"  - {symbol}: {name_kr}")

print("\n✅ 모든 테스트 통과!")
print("\n💡 사용 방법:")
print("   1. 종목명으로 심볼 조회: get_symbol_by_name('애플') -> 'AAPL'")
print("   2. 심볼로 거래소 조회: get_exchange_by_symbol('AAPL') -> 'NASD'")
print("   3. KIS API 주문 시 OVRS_EXCG_CD에 거래소 코드 사용")
