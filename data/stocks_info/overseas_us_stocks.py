"""미국 주식(NASDAQ, NYSE, AMEX) 통합 종목 코드 마스터 데이터"""

import json
import tempfile
import time
import urllib.request
import ssl
import zipfile
from pathlib import Path

import pandas as pd

PROJ_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJ_ROOT / "tmp"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE = CACHE_DIR / "us_stocks_master_cache.json"
LIFETIME = 24 * 3600  # 24시간

# 거래소 코드 매핑 (KIS API 형식)
EXCHANGE_CODES = {
    "nas": "NASD",  # 나스닥
    "nys": "NYSE",  # 뉴욕증권거래소
    "ams": "AMEX",  # 아멕스
}


def _find_member(zf: zipfile.ZipFile, name: str) -> str:
    """Find a ZIP member by case-insensitive name match."""
    lower = name.lower()
    for member in zf.namelist():
        if member.lower() == lower:
            return member
    raise FileNotFoundError(f"{name} not found in ZIP (members: {zf.namelist()})")


def _download_and_parse_us_stocks() -> dict:
    """미국 3개 거래소 MST 파일을 다운로드하고 파싱"""

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        ssl._create_default_https_context = ssl._create_unverified_context

        # 각 거래소별 데이터 수집
        all_data = {
            "name_to_symbol": {},  # 종목명 -> 심볼
            "symbol_to_exchange": {},  # 심볼 -> 거래소 코드
            "symbol_to_name_kr": {},  # 심볼 -> 한글명
            "symbol_to_name_en": {},  # 심볼 -> 영어명
        }

        for file_code, exchange_code in EXCHANGE_CODES.items():
            print(f"{exchange_code} 데이터 다운로드 중...")

            # 다운로드
            zip_path = temp_path / f"{file_code}mst.cod.zip"
            urllib.request.urlretrieve(
                f"https://new.real.download.dws.co.kr/common/master/{file_code}mst.cod.zip",
                str(zip_path),
            )

            # 압축 해제
            with zipfile.ZipFile(zip_path) as zip_file:
                zip_file.extractall(temp_path)
                cod_file = temp_path / _find_member(zip_file, f"{file_code}mst.cod")

            columns = [
                "National code",
                "Exchange id",
                "Exchange code",
                "Exchange name",
                "Symbol",
                "realtime symbol",
                "Korea name",
                "English name",
                "Security type(1:Index,2:Stock,3:ETP(ETF),4:Warrant)",
                "currency",
                "float position",
                "data type",
                "base price",
                "Bid order size",
                "Ask order size",
                "market start time(HHMM)",
                "market end time(HHMM)",
                "DR 여부(Y/N)",
                "DR 국가코드",
                "업종분류코드",
                "지수구성종목 존재 여부(0:구성종목없음,1:구성종목있음)",
                "Tick size Type",
                "구분코드(001:ETF,002:ETN,003:ETC,004:Others,005:VIX Underlying ETF,006:VIX Underlying ETN)",
                "Tick size type 상세",
            ]

            df = pd.read_table(cod_file, sep="\t", encoding="cp949")
            df.columns = columns

            # 데이터 매핑 구축
            for _, row in df.iterrows():
                if pd.notna(row["Symbol"]):
                    symbol = str(row["Symbol"]).strip()
                    if not symbol:
                        continue

                    # 심볼 -> 거래소
                    all_data["symbol_to_exchange"][symbol] = exchange_code

                    # 심볼 -> 이름
                    if pd.notna(row["Korea name"]):
                        korea_name = str(row["Korea name"]).strip()
                        if korea_name:
                            all_data["symbol_to_name_kr"][symbol] = korea_name
                            all_data["name_to_symbol"][korea_name] = symbol

                    if pd.notna(row["English name"]):
                        english_name = str(row["English name"]).strip()
                        if english_name:
                            all_data["symbol_to_name_en"][symbol] = english_name
                            all_data["name_to_symbol"][english_name] = symbol

        return all_data


def _save_cache_data(data: dict) -> None:
    """미국 주식 데이터를 JSON으로 캐시"""
    cache_data = {"data": data, "cached_at": time.time()}
    CACHE_FILE.write_text(
        json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_cached_data() -> dict | None:
    """캐시된 미국 주식 데이터를 로드"""
    if not CACHE_FILE.exists():
        return None

    try:
        cache_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - cache_data["cached_at"] < LIFETIME - 3600:  # 1시간 여유
            return cache_data["data"]
    except (json.JSONDecodeError, KeyError):
        # 캐시 파일이 손상된 경우
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()

    return None


def _initialize_us_stocks_data() -> dict:
    """미국 주식 데이터를 초기화 (캐시 우선, 없으면 다운로드)"""
    # 캐시 확인
    cached_data = _load_cached_data()
    if cached_data:
        print("캐시된 미국 주식 데이터를 로드했습니다.")
        return cached_data

    # 캐시가 없으면 다운로드
    print("미국 주식 마스터 데이터를 다운로드하고 처리합니다...")
    data = _download_and_parse_us_stocks()
    _save_cache_data(data)
    print(f"미국 주식 데이터 처리 완료: {len(data['symbol_to_exchange'])}개 종목")

    return data


# ✅ Lazy loading: 필요할 때만 초기화되는 전역 변수
_us_stocks_data: dict | None = None


def get_us_stocks_data() -> dict:
    """
    미국 주식 데이터를 반환합니다.
    최초 호출 시에만 초기화되며, 이후 호출에는 캐시된 데이터를 반환합니다.

    Returns:
        name_to_symbol, symbol_to_exchange, symbol_to_name_kr, symbol_to_name_en을 포함하는 딕셔너리
    """
    global _us_stocks_data
    if _us_stocks_data is None:
        _us_stocks_data = _initialize_us_stocks_data()
    return _us_stocks_data


def get_exchange_by_symbol(symbol: str) -> str | None:
    """
    심볼로 거래소 코드를 조회합니다.

    Args:
        symbol: 주식 심볼 (예: "AAPL", "TSLA")

    Returns:
        거래소 코드 ("NASD", "NYSE", "AMEX") 또는 None
    """
    data = get_us_stocks_data()
    return data["symbol_to_exchange"].get(symbol)


def get_symbol_by_name(name: str) -> str | None:
    """
    종목명(한글/영어)으로 심볼을 조회합니다.

    Args:
        name: 종목명 (예: "애플", "APPLE INC")

    Returns:
        심볼 (예: "AAPL") 또는 None
    """
    data = get_us_stocks_data()
    return data["name_to_symbol"].get(name)


def get_stock_info(symbol: str) -> dict | None:
    """
    심볼로 전체 종목 정보를 조회합니다.

    Args:
        symbol: 주식 심볼 (예: "AAPL")

    Returns:
        {
            'symbol': 'AAPL',
            'exchange': 'NASD',
            'name_kr': '애플',
            'name_en': 'APPLE INC'
        } 또는 None
    """
    data = get_us_stocks_data()

    if symbol not in data["symbol_to_exchange"]:
        return None

    return {
        "symbol": symbol,
        "exchange": data["symbol_to_exchange"].get(symbol),
        "name_kr": data["symbol_to_name_kr"].get(symbol),
        "name_en": data["symbol_to_name_en"].get(symbol),
    }


# 하위 호환성을 위한 lazy dict 래퍼
class _LazyUSStocksDict:
    """Lazy evaluation을 지원하는 미국 주식 딕셔너리 래퍼"""

    def __init__(self, key: str):
        self._key = key

    def _get_data(self) -> dict:
        return get_us_stocks_data()[self._key]

    def __getitem__(self, key):
        return self._get_data()[key]

    def __contains__(self, key):
        return key in self._get_data()

    def get(self, key, default=None):
        return self._get_data().get(key, default)

    def keys(self):
        return self._get_data().keys()

    def values(self):
        return self._get_data().values()

    def items(self):
        return self._get_data().items()

    def __iter__(self):
        return iter(self._get_data())

    def __len__(self):
        return len(self._get_data())

    def __repr__(self):
        return repr(self._get_data())


# 하위 호환성: 기존 코드가 바로 사용할 수 있도록
US_STOCKS_NAME_TO_SYMBOL = _LazyUSStocksDict("name_to_symbol")
US_STOCKS_SYMBOL_TO_EXCHANGE = _LazyUSStocksDict("symbol_to_exchange")
US_STOCKS_SYMBOL_TO_NAME_KR = _LazyUSStocksDict("symbol_to_name_kr")
US_STOCKS_SYMBOL_TO_NAME_EN = _LazyUSStocksDict("symbol_to_name_en")
