"""
나스닥 주요 종목 심볼 조회 테스트
"""
from data.stocks_info import NASDAQ_NAME_TO_SYMBOL, get_nasdaq_name_to_symbol

print("=" * 70)
print("나스닥 주요 종목 심볼 찾기")
print("=" * 70)

# 전체 데이터 가져오기
nasdaq_data = get_nasdaq_name_to_symbol()

# Apple 찾기
print("\n🍎 Apple 관련 종목:")
apple_matches = {name: symbol for name, symbol in nasdaq_data.items()
                 if 'APPLE' in name.upper() or 'AAPL' in symbol}
for name, symbol in list(apple_matches.items())[:5]:
    print(f"  - {name}: {symbol}")

# Tesla 찾기
print("\n🚗 Tesla 관련 종목:")
tesla_matches = {name: symbol for name, symbol in nasdaq_data.items()
                 if 'TESLA' in name.upper() or 'TSLA' in symbol}
for name, symbol in list(tesla_matches.items())[:5]:
    print(f"  - {name}: {symbol}")

# Microsoft 찾기
print("\n💻 Microsoft 관련 종목:")
msft_matches = {name: symbol for name, symbol in nasdaq_data.items()
                if 'MICROSOFT' in name.upper() or 'MSFT' in symbol}
for name, symbol in list(msft_matches.items())[:5]:
    print(f"  - {name}: {symbol}")

# Amazon 찾기
print("\n📦 Amazon 관련 종목:")
amzn_matches = {name: symbol for name, symbol in nasdaq_data.items()
                if 'AMAZON' in name.upper() or 'AMZN' in symbol}
for name, symbol in list(amzn_matches.items())[:5]:
    print(f"  - {name}: {symbol}")

# Google/Alphabet 찾기
print("\n🔍 Google/Alphabet 관련 종목:")
googl_matches = {name: symbol for name, symbol in nasdaq_data.items()
                 if 'GOOGLE' in name.upper() or 'ALPHABET' in name.upper() or 'GOOGL' in symbol or 'GOOG' in symbol}
for name, symbol in list(googl_matches.items())[:5]:
    print(f"  - {name}: {symbol}")

# 주요 종목 직접 조회
print("\n" + "=" * 70)
print("심볼로 역검색 (Symbol -> Name)")
print("=" * 70)

# 심볼-이름 역매핑 생성
symbol_to_name = {}
for name, symbol in nasdaq_data.items():
    if symbol not in symbol_to_name:
        symbol_to_name[symbol] = []
    symbol_to_name[symbol].append(name)

major_symbols = ["AAPL", "TSLA", "MSFT", "AMZN", "GOOGL", "GOOG", "NVDA", "META"]
for symbol in major_symbols:
    if symbol in symbol_to_name:
        names = symbol_to_name[symbol]
        print(f"\n{symbol}:")
        for name in names[:3]:  # 최대 3개만 출력
            print(f"  - {name}")
    else:
        print(f"\n{symbol}: (없음)")
