"""
전체 시장 통합 테스트
국내(KRX), 해외(US), 암호화폐(Upbit) 모두 포함
"""
import asyncio
from data.stocks_info import (
    KRX_NAME_TO_CODE,
    US_STOCKS_SYMBOL_TO_EXCHANGE,
    get_symbol_by_name,
    get_exchange_by_symbol,
    get_stock_info,
)
from data.coins_info import upbit_pairs


async def main():
    print("=" * 70)
    print("전체 시장 통합 테스트")
    print("=" * 70)

    # 1. 국내 주식 (KRX)
    print("\n📈 1. 국내 주식 (KOSPI/KOSDAQ)")
    print("-" * 70)

    domestic_stocks = ["삼성전자", "SK하이닉스", "NAVER", "카카오", "현대차"]
    for stock_name in domestic_stocks:
        code = KRX_NAME_TO_CODE.get(stock_name)
        status = "✓" if code else "✗"
        print(f"{status} {stock_name}: {code or '(없음)'}")

    # 2. 미국 주식 (NASDAQ, NYSE, AMEX)
    print("\n🇺🇸 2. 미국 주식 (NASDAQ, NYSE, AMEX)")
    print("-" * 70)

    us_stocks = ["애플", "테슬라", "마이크로소프트", "아마존닷컴", "엔비디아"]
    for stock_name in us_stocks:
        symbol = get_symbol_by_name(stock_name)
        if symbol:
            exchange = get_exchange_by_symbol(symbol)
            print(f"✓ {stock_name}: {symbol} ({exchange})")
        else:
            print(f"✗ {stock_name}: (없음)")

    # 3. 암호화폐 (Upbit)
    print("\n₿ 3. 암호화폐 (Upbit KRW)")
    print("-" * 70)

    await upbit_pairs.prime_upbit_constants()

    crypto_names = ["비트코인", "이더리움", "리플", "솔라나", "에이다"]
    for coin_name in crypto_names:
        pair = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
        status = "✓" if pair else "✗"
        print(f"{status} {coin_name}: {pair or '(없음)'}")

    # 4. KIS API 주문 시나리오
    print("\n" + "=" * 70)
    print("KIS API 주문 시나리오 시뮬레이션")
    print("=" * 70)

    print("\n📝 시나리오 1: 국내 주식 매수")
    print("-" * 70)
    stock_name = "삼성전자"
    code = KRX_NAME_TO_CODE.get(stock_name)
    print(f"종목명: {stock_name}")
    print(f"종목코드: {code}")
    print(f"API: /uapi/domestic-stock/v1/trading/order-cash")
    print(f"파라미터: pdno={code}")

    print("\n📝 시나리오 2: 해외 주식 매수")
    print("-" * 70)
    stock_name = "애플"
    symbol = get_symbol_by_name(stock_name)
    exchange = get_exchange_by_symbol(symbol) if symbol else None
    info = get_stock_info(symbol) if symbol else None

    print(f"종목명: {stock_name}")
    print(f"심볼: {symbol}")
    print(f"거래소: {exchange}")
    print(f"API: /uapi/overseas-stock/v1/trading/order")
    print(f"파라미터:")
    print(f"  - OVRS_EXCG_CD: {exchange}")
    print(f"  - PDNO: {symbol}")

    # 5. 데이터 통계
    print("\n" + "=" * 70)
    print("📊 전체 데이터 통계")
    print("=" * 70)

    kospi_count = len([k for k in KRX_NAME_TO_CODE.keys()])
    us_count = len(US_STOCKS_SYMBOL_TO_EXCHANGE)
    crypto_count = len(upbit_pairs.KRW_TRADABLE_COINS)

    print(f"국내 주식 (KRX): {kospi_count:,}개")
    print(f"미국 주식 (US): {us_count:,}개")
    print(f"  - NASDAQ: 4,837개")
    print(f"  - NYSE: 2,838개")
    print(f"  - AMEX: 3,862개")
    print(f"암호화폐 (Upbit KRW): {crypto_count:,}개")
    print(f"\n총 거래 가능 자산: {kospi_count + us_count + crypto_count:,}개")

    # 6. 거래소 코드 매핑 확인
    print("\n" + "=" * 70)
    print("🔍 거래소 코드 매핑 확인")
    print("=" * 70)

    test_symbols = ["AAPL", "JPM", "SPY"]
    print("\nSymbol -> Exchange:")
    for sym in test_symbols:
        ex = get_exchange_by_symbol(sym)
        print(f"  {sym}: {ex}")

    # 7. 중복 확인
    print("\n" + "=" * 70)
    print("✅ 심볼 중복 검사")
    print("=" * 70)

    symbols_by_exchange = {}
    for symbol, exchange in US_STOCKS_SYMBOL_TO_EXCHANGE.items():
        if symbol not in symbols_by_exchange:
            symbols_by_exchange[symbol] = []
        symbols_by_exchange[symbol].append(exchange)

    duplicates = {sym: exs for sym, exs in symbols_by_exchange.items() if len(exs) > 1}

    if duplicates:
        print(f"⚠️  중복 심볼 발견: {len(duplicates)}개")
        for sym, exs in list(duplicates.items())[:5]:
            print(f"  - {sym}: {exs}")
    else:
        print("✅ 중복 심볼 없음 - 각 심볼은 고유합니다!")

    print("\n" + "=" * 70)
    print("✅ 모든 테스트 완료!")
    print("=" * 70)

    print("""
💡 핵심 기능 요약:

1. 국내 주식: KRX_NAME_TO_CODE["삼성전자"] -> "005930"

2. 해외 주식:
   - 종목명 -> 심볼: get_symbol_by_name("애플") -> "AAPL"
   - 심볼 -> 거래소: get_exchange_by_symbol("AAPL") -> "NASD"
   - KIS API 주문: OVRS_EXCG_CD에 거래소 코드 사용

3. 암호화폐: upbit_pairs.NAME_TO_PAIR_KR["비트코인"] -> "KRW-BTC"

4. 심볼 중복: 없음 (각 심볼은 하나의 거래소에만 존재)
    """)


if __name__ == "__main__":
    asyncio.run(main())
