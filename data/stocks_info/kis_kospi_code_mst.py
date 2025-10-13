'''코스피주식종목코드(kospi_code.mst) 정제 파이썬 파일'''
import json
import tempfile
import time
import urllib.request
import ssl
import zipfile
import os
from pathlib import Path

import pandas as pd

PROJ_ROOT = Path(__file__).resolve().parents[2]  # 프로젝트 루트로 조정 필요
CACHE_DIR = PROJ_ROOT / "tmp"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE = CACHE_DIR / "kospi_master_cache.json"
LIFETIME = 24 * 3600  # 24 h


def _download_and_parse_kospi_master() -> dict[str, str]:
    """MST 파일을 다운로드하고 파싱하여 종목명-코드 매핑을 반환"""

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # SSL 설정
        ssl._create_default_https_context = ssl._create_unverified_context

        # 다운로드
        zip_path = temp_path / "kospi_code.zip"
        urllib.request.urlretrieve(
            "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
            str(zip_path)
        )

        # 압축 해제
        with zipfile.ZipFile(zip_path) as kospi_zip:
            kospi_zip.extractall(temp_path)

        # MST 파일 파싱
        mst_file = temp_path / "kospi_code.mst"
        tmp_fil1 = temp_path / "kospi_code_part1.tmp"
        tmp_fil2 = temp_path / "kospi_code_part2.tmp"

        # Part 1, 2로 분리
        with open(mst_file, mode="r", encoding="cp949") as f:
            with open(tmp_fil1, mode="w", encoding="utf-8") as wf1:
                with open(tmp_fil2, mode="w", encoding="utf-8") as wf2:
                    for row in f:
                        rf1 = row[0:len(row) - 228]
                        rf1_1 = rf1[0:9].rstrip()
                        rf1_2 = rf1[9:21].rstrip()
                        rf1_3 = rf1[21:].strip()
                        wf1.write(rf1_1 + ',' + rf1_2 + ',' + rf1_3 + '\n')
                        rf2 = row[-228:]
                        wf2.write(rf2)

        # DataFrame으로 변환
        part1_columns = ['단축코드', '표준코드', '한글명']
        df1 = pd.read_csv(tmp_fil1, header=None, names=part1_columns)

        field_specs = [2, 1, 4, 4, 4,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 9, 5, 5, 1,
                       1, 1, 2, 1, 1,
                       1, 2, 2, 2, 3,
                       1, 3, 12, 12, 8,
                       15, 21, 2, 7, 1,
                       1, 1, 1, 1, 9,
                       9, 9, 5, 9, 8,
                       9, 3, 1, 1, 1]

        part2_columns = ['그룹코드', '시가총액규모', '지수업종대분류', '지수업종중분류', '지수업종소분류',
                         '제조업', '저유동성', '지배구조지수종목', 'KOSPI200섹터업종', 'KOSPI100',
                         'KOSPI50', 'KRX', 'ETP', 'ELW발행', 'KRX100',
                         'KRX자동차', 'KRX반도체', 'KRX바이오', 'KRX은행', 'SPAC',
                         'KRX에너지화학', 'KRX철강', '단기과열', 'KRX미디어통신', 'KRX건설',
                         'Non1', 'KRX증권', 'KRX선박', 'KRX섹터_보험', 'KRX섹터_운송',
                         'SRI', '기준가', '매매수량단위', '시간외수량단위', '거래정지',
                         '정리매매', '관리종목', '시장경고', '경고예고', '불성실공시',
                         '우회상장', '락구분', '액면변경', '증자구분', '증거금비율',
                         '신용가능', '신용기간', '전일거래량', '액면가', '상장일자',
                         '상장주수', '자본금', '결산월', '공모가', '우선주',
                         '공매도과열', '이상급등', 'KRX300', 'KOSPI', '매출액',
                         '영업이익', '경상이익', '당기순이익', 'ROE', '기준년월',
                         '시가총액', '그룹사코드', '회사신용한도초과', '담보대출가능', '대주가능']

        df2 = pd.read_fwf(tmp_fil2, widths=field_specs, names=part2_columns)
        df = pd.merge(df1, df2, how='outer', left_index=True, right_index=True)

        # 종목명-코드 매핑 생성
        name_to_code = (
            df.loc[df["한글명"].notna(), ["단축코드", "한글명"]]
            .assign(단축코드=lambda d: d["단축코드"].astype(str).str.zfill(6))
            .set_index("한글명")["단축코드"]
            .str.strip()
            .to_dict()
        )

        return name_to_code


def _save_cache_data(name_to_code: dict[str, str]) -> None:
    """KOSPI 데이터를 JSON으로 캐시"""
    cache_data = {
        "name_to_code": name_to_code,
        "cached_at": time.time()
    }
    CACHE_FILE.write_text(
        json.dumps(cache_data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def _load_cached_data() -> dict[str, str] | None:
    """캐시된 KOSPI 데이터를 로드"""
    if not CACHE_FILE.exists():
        return None

    try:
        data = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        if time.time() - data["cached_at"] < LIFETIME - 3600:  # 1시간 여유
            return data["name_to_code"]
    except (json.JSONDecodeError, KeyError):
        # 캐시 파일이 손상된 경우
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()

    return None


def _initialize_kospi_data() -> dict[str, str]:
    """KOSPI 데이터를 초기화 (캐시 우선, 없으면 다운로드)"""
    # 캐시 확인
    cached_data = _load_cached_data()
    if cached_data:
        print("캐시된 KOSPI 데이터를 로드했습니다.")
        return cached_data

    # 캐시가 없으면 다운로드
    print("KOSPI 마스터 데이터를 다운로드하고 처리합니다...")
    name_to_code = _download_and_parse_kospi_master()
    _save_cache_data(name_to_code)
    print(f"KOSPI 데이터 처리 완료: {len(name_to_code)}개 종목")

    return name_to_code


# ✅ Lazy loading: 필요할 때만 초기화되는 전역 변수
_kospi_name_to_code: dict[str, str] | None = None


def get_kospi_name_to_code() -> dict[str, str]:
    """
    KOSPI 종목명-코드 매핑을 반환합니다.
    최초 호출 시에만 초기화되며, 이후 호출에는 캐시된 데이터를 반환합니다.

    Returns:
        종목명을 키로, 종목코드를 값으로 하는 딕셔너리
    """
    global _kospi_name_to_code
    if _kospi_name_to_code is None:
        _kospi_name_to_code = _initialize_kospi_data()
    return _kospi_name_to_code


# 하위 호환성을 위한 속성 (lazy evaluation)
class _LazyKOSPIDict:
    """Lazy evaluation을 지원하는 KOSPI 딕셔너리 래퍼"""
    def __getitem__(self, key):
        return get_kospi_name_to_code()[key]

    def __contains__(self, key):
        return key in get_kospi_name_to_code()

    def get(self, key, default=None):
        return get_kospi_name_to_code().get(key, default)

    def keys(self):
        return get_kospi_name_to_code().keys()

    def values(self):
        return get_kospi_name_to_code().values()

    def items(self):
        return get_kospi_name_to_code().items()

    def __iter__(self):
        return iter(get_kospi_name_to_code())

    def __len__(self):
        return len(get_kospi_name_to_code())

    def __repr__(self):
        return repr(get_kospi_name_to_code())


# 하위 호환성: 기존 코드가 KOSPI_NAME_TO_CODE를 바로 사용할 수 있도록
KOSPI_NAME_TO_CODE = _LazyKOSPIDict()
