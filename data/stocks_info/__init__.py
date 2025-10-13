# app/data/stocks_info/__init__.py
"""KIS 종목·섹터 코드 마스터 모음."""

from .kis_kospi_code_mst import KOSPI_NAME_TO_CODE, get_kospi_name_to_code
from .kis_kosdaq_code_mst import KOSDAQ_NAME_TO_CODE, get_kosdaq_name_to_code
from .overseas_nasdaq_code import NASDAQ_NAME_TO_SYMBOL, get_nasdaq_name_to_symbol
from .overseas_us_stocks import (
    US_STOCKS_NAME_TO_SYMBOL,
    US_STOCKS_SYMBOL_TO_EXCHANGE,
    US_STOCKS_SYMBOL_TO_NAME_KR,
    US_STOCKS_SYMBOL_TO_NAME_EN,
    get_us_stocks_data,
    get_exchange_by_symbol,
    get_symbol_by_name,
    get_stock_info,
)

# from .sector_code import SECTOR_CODE_DICT

# Lazy evaluation을 지원하는 KRX 통합 딕셔너리
class _LazyKRXDict:
    """KOSPI와 KOSDAQ을 통합한 Lazy evaluation 딕셔너리"""
    def __getitem__(self, key):
        # KOSPI 먼저 확인, 없으면 KOSDAQ 확인
        kospi = get_kospi_name_to_code()
        if key in kospi:
            return kospi[key]
        kosdaq = get_kosdaq_name_to_code()
        return kosdaq[key]

    def __contains__(self, key):
        return key in get_kospi_name_to_code() or key in get_kosdaq_name_to_code()

    def get(self, key, default=None):
        kospi = get_kospi_name_to_code()
        if key in kospi:
            return kospi[key]
        kosdaq = get_kosdaq_name_to_code()
        return kosdaq.get(key, default)

    def keys(self):
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()
        return list(kospi.keys()) + list(kosdaq.keys())

    def values(self):
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()
        return list(kospi.values()) + list(kosdaq.values())

    def items(self):
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()
        return list(kospi.items()) + list(kosdaq.items())

    def __iter__(self):
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()
        return iter(list(kospi.keys()) + list(kosdaq.keys()))

    def __len__(self):
        return len(get_kospi_name_to_code()) + len(get_kosdaq_name_to_code())

    def __repr__(self):
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()
        return repr({**kospi, **kosdaq})


KRX_NAME_TO_CODE = _LazyKRXDict()


def prime_krx_stock_data():
    """
    KRX 종목 데이터를 명시적으로 초기화합니다.
    Upbit의 prime_upbit_constants()와 유사한 역할을 합니다.

    이 함수를 호출하면 KOSPI와 KOSDAQ 데이터가 모두 로드됩니다.
    호출하지 않아도 데이터 접근 시 자동으로 로드되지만,
    시작 시 명시적으로 호출하면 초기화 시점을 제어할 수 있습니다.
    """
    print("KRX 종목 데이터를 초기화합니다...")
    kospi_count = len(get_kospi_name_to_code())
    kosdaq_count = len(get_kosdaq_name_to_code())
    print(f"초기화 완료: KOSPI {kospi_count}개, KOSDAQ {kosdaq_count}개 종목")


def prime_nasdaq_stock_data():
    """
    나스닥 종목 데이터를 명시적으로 초기화합니다.

    이 함수를 호출하면 나스닥 데이터가 로드됩니다.
    호출하지 않아도 데이터 접근 시 자동으로 로드되지만,
    시작 시 명시적으로 호출하면 초기화 시점을 제어할 수 있습니다.
    """
    print("나스닥 종목 데이터를 초기화합니다...")
    nasdaq_count = len(get_nasdaq_name_to_symbol())
    print(f"초기화 완료: 나스닥 {nasdaq_count}개 종목")


def prime_us_stocks_data():
    """
    미국 주식(NASDAQ, NYSE, AMEX) 데이터를 명시적으로 초기화합니다.

    이 함수를 호출하면 미국 3대 거래소 데이터가 모두 로드됩니다.
    호출하지 않아도 데이터 접근 시 자동으로 로드되지만,
    시작 시 명시적으로 호출하면 초기화 시점을 제어할 수 있습니다.
    """
    print("미국 주식 데이터를 초기화합니다...")
    data = get_us_stocks_data()
    print(f"초기화 완료: {len(data['symbol_to_exchange'])}개 종목 (NASDAQ, NYSE, AMEX)")


__all__ = [
    # KRX (국내)
    "KOSPI_NAME_TO_CODE",
    "KOSDAQ_NAME_TO_CODE",
    "KRX_NAME_TO_CODE",
    "get_kospi_name_to_code",
    "get_kosdaq_name_to_code",
    "prime_krx_stock_data",
    # NASDAQ (단일 거래소)
    "NASDAQ_NAME_TO_SYMBOL",
    "get_nasdaq_name_to_symbol",
    "prime_nasdaq_stock_data",
    # US Stocks (통합: NASDAQ, NYSE, AMEX)
    "US_STOCKS_NAME_TO_SYMBOL",
    "US_STOCKS_SYMBOL_TO_EXCHANGE",
    "US_STOCKS_SYMBOL_TO_NAME_KR",
    "US_STOCKS_SYMBOL_TO_NAME_EN",
    "get_us_stocks_data",
    "get_exchange_by_symbol",
    "get_symbol_by_name",
    "get_stock_info",
    "prime_us_stocks_data",
]
