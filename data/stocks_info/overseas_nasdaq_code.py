"""나스닥 주식 종목 코드 마스터 데이터"""
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
CACHE_FILE = CACHE_DIR / "nasdaq_master_cache.json"
LIFETIME = 24 * 3600  # 24시간


def _download_and_parse_nasdaq_master() -> dict[str, str]:
    """나스닥 MST 파일을 다운로드하고 파싱하여 종목명-심볼 매핑을 반환"""

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # SSL 설정
        ssl._create_default_https_context = ssl._create_unverified_context

        # 다운로드
        zip_path = temp_path / "nasmst.cod.zip"
        urllib.request.urlretrieve(
            "https://new.real.download.dws.co.kr/common/master/nasmst.cod.zip",
            str(zip_path)
        )

        # 압축 해제
        with zipfile.ZipFile(zip_path) as nasdaq_zip:
            nasdaq_zip.extractall(temp_path)

        # MST 파일 파싱
        cod_file = temp_path / "nasmst.cod"

        # 칼럼 정의
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

        # DataFrame으로 읽기
        df = pd.read_table(cod_file, sep='\t', encoding='cp949')
        df.columns = columns

        # 종목명-심볼 매핑 생성 (한글명과 영어명 모두 지원)
        name_to_symbol = {}

        # 한글명 -> 심볼
        for _, row in df.iterrows():
            if pd.notna(row['Korea name']) and pd.notna(row['Symbol']):
                korea_name = str(row['Korea name']).strip()
                symbol = str(row['Symbol']).strip()
                if korea_name and symbol:
                    name_to_symbol[korea_name] = symbol

            # 영어명 -> 심볼 (한글명이 없거나 추가 참조용)
            if pd.notna(row['English name']) and pd.notna(row['Symbol']):
                english_name = str(row['English name']).strip()
                symbol = str(row['Symbol']).strip()
                if english_name and symbol:
                    name_to_symbol[english_name] = symbol

        return name_to_symbol


def _save_cache_data(name_to_symbol: dict[str, str]) -> None:
    """나스닥 데이터를 JSON으로 캐시"""
    cache_data = {
        "name_to_symbol": name_to_symbol,
        "cached_at": time.time()
    }
    CACHE_FILE.write_text(
        json.dumps(cache_data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def _load_cached_data() -> dict[str, str] | None:
    """캐시된 나스닥 데이터를 로드"""
    if not CACHE_FILE.exists():
        return None

    try:
        data = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        if time.time() - data["cached_at"] < LIFETIME - 3600:  # 1시간 여유
            return data["name_to_symbol"]
    except (json.JSONDecodeError, KeyError):
        # 캐시 파일이 손상된 경우
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()

    return None


def _initialize_nasdaq_data() -> dict[str, str]:
    """나스닥 데이터를 초기화 (캐시 우선, 없으면 다운로드)"""
    # 캐시 확인
    cached_data = _load_cached_data()
    if cached_data:
        print("캐시된 나스닥 데이터를 로드했습니다.")
        return cached_data

    # 캐시가 없으면 다운로드
    print("나스닥 마스터 데이터를 다운로드하고 처리합니다...")
    name_to_symbol = _download_and_parse_nasdaq_master()
    _save_cache_data(name_to_symbol)
    print(f"나스닥 데이터 처리 완료: {len(name_to_symbol)}개 종목")

    return name_to_symbol


# ✅ Lazy loading: 필요할 때만 초기화되는 전역 변수
_nasdaq_name_to_symbol: dict[str, str] | None = None


def get_nasdaq_name_to_symbol() -> dict[str, str]:
    """
    나스닥 종목명-심볼 매핑을 반환합니다.
    최초 호출 시에만 초기화되며, 이후 호출에는 캐시된 데이터를 반환합니다.

    Returns:
        종목명(한글/영어)을 키로, 심볼을 값으로 하는 딕셔너리
    """
    global _nasdaq_name_to_symbol
    if _nasdaq_name_to_symbol is None:
        _nasdaq_name_to_symbol = _initialize_nasdaq_data()
    return _nasdaq_name_to_symbol


# 하위 호환성을 위한 lazy dict 래퍼
class _LazyNASDAQDict:
    """Lazy evaluation을 지원하는 나스닥 딕셔너리 래퍼"""

    def __getitem__(self, key):
        return get_nasdaq_name_to_symbol()[key]

    def __contains__(self, key):
        return key in get_nasdaq_name_to_symbol()

    def get(self, key, default=None):
        return get_nasdaq_name_to_symbol().get(key, default)

    def keys(self):
        return get_nasdaq_name_to_symbol().keys()

    def values(self):
        return get_nasdaq_name_to_symbol().values()

    def items(self):
        return get_nasdaq_name_to_symbol().items()

    def __iter__(self):
        return iter(get_nasdaq_name_to_symbol())

    def __len__(self):
        return len(get_nasdaq_name_to_symbol())

    def __repr__(self):
        return repr(get_nasdaq_name_to_symbol())


# 하위 호환성: 기존 코드가 NASDAQ_NAME_TO_SYMBOL을 바로 사용할 수 있도록
NASDAQ_NAME_TO_SYMBOL = _LazyNASDAQDict()
