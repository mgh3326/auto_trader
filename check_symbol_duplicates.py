"""
NASDAQ, NYSE, AMEX 심볼 중복 검사

동일한 심볼이 여러 거래소에 존재하는지 확인
"""
import urllib.request
import ssl
import zipfile
import tempfile
from pathlib import Path
import pandas as pd
from collections import defaultdict

ssl._create_default_https_context = ssl._create_unverified_context


def download_and_parse_exchange(exchange_code: str) -> pd.DataFrame:
    """특정 거래소의 MST 파일 다운로드 및 파싱"""
    print(f"다운로드 중: {exchange_code}...")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # 다운로드
        zip_path = temp_path / f"{exchange_code}mst.cod.zip"
        urllib.request.urlretrieve(
            f"https://new.real.download.dws.co.kr/common/master/{exchange_code}mst.cod.zip",
            str(zip_path)
        )

        # 압축 해제
        with zipfile.ZipFile(zip_path) as zip_file:
            zip_file.extractall(temp_path)

        # MST 파일 읽기
        cod_file = temp_path / f"{exchange_code}mst.cod"

        columns = [
            'National code', 'Exchange id', 'Exchange code', 'Exchange name',
            'Symbol', 'realtime symbol', 'Korea name', 'English name',
            'Security type(1:Index,2:Stock,3:ETP(ETF),4:Warrant)',
            'currency', 'float position', 'data type', 'base price',
            'Bid order size', 'Ask order size',
            'market start time(HHMM)', 'market end time(HHMM)',
            'DR 여부(Y/N)', 'DR 국가코드', '업종분류코드',
            '지수구성종목 존재 여부(0:구성종목없음,1:구성종목있음)',
            'Tick size Type',
            '구분코드(001:ETF,002:ETN,003:ETC,004:Others,005:VIX Underlying ETF,006:VIX Underlying ETN)',
            'Tick size type 상세'
        ]

        df = pd.read_table(cod_file, sep='\t', encoding='cp949')
        df.columns = columns
        df['exchange'] = exchange_code.upper()  # 거래소 코드 추가

        return df


def main():
    print("=" * 70)
    print("미국 주요 거래소 심볼 중복 검사")
    print("=" * 70)

    # 3개 거래소 데이터 수집
    exchanges = {
        'NASD': download_and_parse_exchange('nas'),
        'NYSE': download_and_parse_exchange('nys'),
        'AMEX': download_and_parse_exchange('ams'),
    }

    print("\n" + "=" * 70)
    print("거래소별 종목 수")
    print("=" * 70)
    for exchange, df in exchanges.items():
        print(f"{exchange}: {len(df):,}개")

    # 심볼별 거래소 매핑
    symbol_to_exchanges = defaultdict(list)

    for exchange, df in exchanges.items():
        for _, row in df.iterrows():
            if pd.notna(row['Symbol']):
                symbol = str(row['Symbol']).strip()
                if symbol:
                    symbol_to_exchanges[symbol].append({
                        'exchange': exchange,
                        'korea_name': str(row['Korea name']).strip() if pd.notna(row['Korea name']) else None,
                        'english_name': str(row['English name']).strip() if pd.notna(row['English name']) else None,
                    })

    # 중복 심볼 찾기
    duplicates = {symbol: infos for symbol, infos in symbol_to_exchanges.items() if len(infos) > 1}

    print("\n" + "=" * 70)
    print("중복 심볼 분석")
    print("=" * 70)

    if duplicates:
        print(f"⚠️  중복된 심볼 발견: {len(duplicates)}개\n")

        # 상위 20개만 출력
        print("중복 심볼 예시 (처음 20개):")
        for i, (symbol, infos) in enumerate(list(duplicates.items())[:20]):
            print(f"\n{i+1}. {symbol} ({len(infos)}개 거래소)")
            for info in infos:
                print(f"   - {info['exchange']}: {info['korea_name']} / {info['english_name']}")

        # 통계
        print("\n" + "=" * 70)
        print("중복 통계")
        print("=" * 70)
        dup_counts = defaultdict(int)
        for infos in duplicates.values():
            dup_counts[len(infos)] += 1

        for count, num in sorted(dup_counts.items()):
            print(f"{count}개 거래소에 존재: {num}개 심볼")

    else:
        print("✅ 중복된 심볼이 없습니다!")

    # 총 유니크 심볼 수
    total_unique = len(symbol_to_exchanges)
    total_all = sum(len(df) for df in exchanges.values())

    print("\n" + "=" * 70)
    print("요약")
    print("=" * 70)
    print(f"전체 종목 수: {total_all:,}개")
    print(f"유니크 심볼 수: {total_unique:,}개")
    print(f"중복 심볼 수: {len(duplicates):,}개")

    # 결론
    print("\n" + "=" * 70)
    print("🎯 결론")
    print("=" * 70)
    if duplicates:
        print("""
⚠️  심볼이 중복됩니다!

따라서 SYMBOL_TO_EXCHANGE 매핑을 만들어야 합니다:
- SYMBOL만으로는 거래소를 특정할 수 없음
- Symbol -> Exchange Code 매핑 딕셔너리 필요
- 또는 (Symbol, Exchange) 튜플을 키로 사용
        """)
    else:
        print("""
✅ 심볼이 중복되지 않습니다!

각 심볼은 하나의 거래소에만 존재하므로:
- SYMBOL만으로 종목을 유일하게 식별 가능
- 거래소 코드 매핑이 필요함
        """)


if __name__ == "__main__":
    main()
