'''코스닥주식종목코드(kosdaq_code.mst) 정제 파이썬 파일'''
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
CACHE_FILE = CACHE_DIR / "kosdaq_master_cache.json"
LIFETIME = 24 * 3600  # 24 h


def kosdaq_master_download(base_dir, verbose=False):
    cwd = os.getcwd()
    if (verbose): print(f"current directory is {cwd}")
    ssl._create_default_https_context = ssl._create_unverified_context

    urllib.request.urlretrieve("https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
                               base_dir + "\\kosdaq_code.zip")

    os.chdir(base_dir)
    if (verbose): print(f"change directory to {base_dir}")
    kosdaq_zip = zipfile.ZipFile('kosdaq_code.zip')
    kosdaq_zip.extractall()

    kosdaq_zip.close()

    if os.path.exists("kosdaq_code.zip"):
        os.remove("kosdaq_code.zip")


def get_kosdaq_master_dataframe(base_dir):
    file_name = base_dir + "\\kosdaq_code.mst"
    tmp_fil1 = base_dir + "\\kosdaq_code_part1.tmp"
    tmp_fil2 = base_dir + "\\kosdaq_code_part2.tmp"

    wf1 = open(tmp_fil1, mode="w")
    wf2 = open(tmp_fil2, mode="w")

    with open(file_name, mode="r", encoding="cp949") as f:
        for row in f:
            rf1 = row[0:len(row) - 222]
            rf1_1 = rf1[0:9].rstrip()
            rf1_2 = rf1[9:21].rstrip()
            rf1_3 = rf1[21:].strip()
            wf1.write(rf1_1 + ',' + rf1_2 + ',' + rf1_3 + '\n')
            rf2 = row[-222:]
            wf2.write(rf2)

    wf1.close()
    wf2.close()

    part1_columns = ['단축코드', '표준코드', '한글종목명']
    df1 = pd.read_csv(tmp_fil1, header=None, names=part1_columns, encoding='cp949')

    field_specs = [2, 1,
                   4, 4, 4, 1, 1,
                   1, 1, 1, 1, 1,
                   1, 1, 1, 1, 1,
                   1, 1, 1, 1, 1,
                   1, 1, 1, 1, 9,
                   5, 5, 1, 1, 1,
                   2, 1, 1, 1, 2,
                   2, 2, 3, 1, 3,
                   12, 12, 8, 15, 21,
                   2, 7, 1, 1, 1,
                   1, 9, 9, 9, 5,
                   9, 8, 9, 3, 1,
                   1, 1
                   ]

    part2_columns = ['증권그룹구분코드', '시가총액 규모 구분 코드 유가',
                     '지수업종 대분류 코드', '지수 업종 중분류 코드', '지수업종 소분류 코드', '벤처기업 여부 (Y/N)',
                     '저유동성종목 여부', 'KRX 종목 여부', 'ETP 상품구분코드', 'KRX100 종목 여부 (Y/N)',
                     'KRX 자동차 여부', 'KRX 반도체 여부', 'KRX 바이오 여부', 'KRX 은행 여부', '기업인수목적회사여부',
                     'KRX 에너지 화학 여부', 'KRX 철강 여부', '단기과열종목구분코드', 'KRX 미디어 통신 여부',
                     'KRX 건설 여부', '(코스닥)투자주의환기종목여부', 'KRX 증권 구분', 'KRX 선박 구분',
                     'KRX섹터지수 보험여부', 'KRX섹터지수 운송여부', 'KOSDAQ150지수여부 (Y,N)', '주식 기준가',
                     '정규 시장 매매 수량 단위', '시간외 시장 매매 수량 단위', '거래정지 여부', '정리매매 여부',
                     '관리 종목 여부', '시장 경고 구분 코드', '시장 경고위험 예고 여부', '불성실 공시 여부',
                     '우회 상장 여부', '락구분 코드', '액면가 변경 구분 코드', '증자 구분 코드', '증거금 비율',
                     '신용주문 가능 여부', '신용기간', '전일 거래량', '주식 액면가', '주식 상장 일자', '상장 주수(천)',
                     '자본금', '결산 월', '공모 가격', '우선주 구분 코드', '공매도과열종목여부', '이상급등종목여부',
                     'KRX300 종목 여부 (Y/N)', '매출액', '영업이익', '경상이익', '단기순이익', 'ROE(자기자본이익률)',
                     '기준년월', '전일기준 시가총액 (억)', '그룹사 코드', '회사신용한도초과여부', '담보대출가능여부', '대주가능여부'
                     ]

    df2 = pd.read_fwf(tmp_fil2, widths=field_specs, names=part2_columns)

    df = pd.merge(df1, df2, how='outer', left_index=True, right_index=True)

    # clean temporary file and dataframe
    del (df1)
    del (df2)
    os.remove(tmp_fil1)
    os.remove(tmp_fil2)

    print("Done")

    return df


def _download_and_parse_kosdaq_master() -> dict[str, str]:
    """MST 파일을 다운로드하고 파싱하여 종목명-코드 매핑을 반환"""

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # SSL 설정
        ssl._create_default_https_context = ssl._create_unverified_context

        # 다운로드
        zip_path = temp_path / "kosdaq_code.zip"
        urllib.request.urlretrieve(
            "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
            str(zip_path)
        )

        # 압축 해제
        with zipfile.ZipFile(zip_path) as kosdaq_zip:
            kosdaq_zip.extractall(temp_path)

        # MST 파일 파싱
        mst_file = temp_path / "kosdaq_code.mst"
        tmp_fil1 = temp_path / "kosdaq_code_part1.tmp"
        tmp_fil2 = temp_path / "kosdaq_code_part2.tmp"

        # Part 1, 2로 분리
        with open(mst_file, mode="r", encoding="cp949") as f:
            with open(tmp_fil1, mode="w", encoding="utf-8") as wf1:
                with open(tmp_fil2, mode="w", encoding="utf-8") as wf2:
                    for row in f:
                        rf1 = row[0:len(row) - 222]
                        rf1_1 = rf1[0:9].rstrip()
                        rf1_2 = rf1[9:21].rstrip()
                        rf1_3 = rf1[21:].strip()
                        wf1.write(rf1_1 + ',' + rf1_2 + ',' + rf1_3 + '\n')
                        rf2 = row[-222:]
                        wf2.write(rf2)

        # DataFrame으로 변환
        part1_columns = ['단축코드', '표준코드', '한글명']

        df1 = pd.read_csv(tmp_fil1, header=None, names=part1_columns)

        field_specs = [2, 1,
                       4, 4, 4, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 1,
                       1, 1, 1, 1, 9,
                       5, 5, 1, 1, 1,
                       2, 1, 1, 1, 2,
                       2, 2, 3, 1, 3,
                       12, 12, 8, 15, 21,
                       2, 7, 1, 1, 1,
                       1, 9, 9, 9, 5,
                       9, 8, 9, 3, 1,
                       1, 1
                       ]

        part2_columns = ['증권그룹구분코드', '시가총액 규모 구분 코드 유가',
                         '지수업종 대분류 코드', '지수 업종 중분류 코드', '지수업종 소분류 코드', '벤처기업 여부 (Y/N)',
                         '저유동성종목 여부', 'KRX 종목 여부', 'ETP 상품구분코드', 'KRX100 종목 여부 (Y/N)',
                         'KRX 자동차 여부', 'KRX 반도체 여부', 'KRX 바이오 여부', 'KRX 은행 여부', '기업인수목적회사여부',
                         'KRX 에너지 화학 여부', 'KRX 철강 여부', '단기과열종목구분코드', 'KRX 미디어 통신 여부',
                         'KRX 건설 여부', '(코스닥)투자주의환기종목여부', 'KRX 증권 구분', 'KRX 선박 구분',
                         'KRX섹터지수 보험여부', 'KRX섹터지수 운송여부', 'KOSDAQ150지수여부 (Y,N)', '주식 기준가',
                         '정규 시장 매매 수량 단위', '시간외 시장 매매 수량 단위', '거래정지 여부', '정리매매 여부',
                         '관리 종목 여부', '시장 경고 구분 코드', '시장 경고위험 예고 여부', '불성실 공시 여부',
                         '우회 상장 여부', '락구분 코드', '액면가 변경 구분 코드', '증자 구분 코드', '증거금 비율',
                         '신용주문 가능 여부', '신용기간', '전일 거래량', '주식 액면가', '주식 상장 일자', '상장 주수(천)',
                         '자본금', '결산 월', '공모 가격', '우선주 구분 코드', '공매도과열종목여부', '이상급등종목여부',
                         'KRX300 종목 여부 (Y/N)', '매출액', '영업이익', '경상이익', '단기순이익', 'ROE(자기자본이익률)',
                         '기준년월', '전일기준 시가총액 (억)', '그룹사 코드', '회사신용한도초과여부', '담보대출가능여부', '대주가능여부'
                         ]

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
    """KOSDAQ 데이터를 JSON으로 캐시"""
    cache_data = {
        "name_to_code": name_to_code,
        "cached_at": time.time()
    }
    CACHE_FILE.write_text(
        json.dumps(cache_data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def _load_cached_data() -> dict[str, str] | None:
    """캐시된 KOSDAQ 데이터를 로드"""
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


def _initialize_kosdaq_data() -> dict[str, str]:
    """KOSDAQ 데이터를 초기화 (캐시 우선, 없으면 다운로드)"""
    # 캐시 확인
    cached_data = _load_cached_data()
    if cached_data:
        print("캐시된 KOSDAQ 데이터를 로드했습니다.")
        return cached_data

    # 캐시가 없으면 다운로드
    print("KOSDAQ 마스터 데이터를 다운로드하고 처리합니다...")
    name_to_code = _download_and_parse_kosdaq_master()
    _save_cache_data(name_to_code)
    print(f"KOSDAQ 데이터 처리 완료: {len(name_to_code)}개 종목")

    return name_to_code


# ✅ Lazy loading: 필요할 때만 초기화되는 전역 변수
_kosdaq_name_to_code: dict[str, str] | None = None


def get_kosdaq_name_to_code() -> dict[str, str]:
    """
    KOSDAQ 종목명-코드 매핑을 반환합니다.
    최초 호출 시에만 초기화되며, 이후 호출에는 캐시된 데이터를 반환합니다.

    Returns:
        종목명을 키로, 종목코드를 값으로 하는 딕셔너리
    """
    global _kosdaq_name_to_code
    if _kosdaq_name_to_code is None:
        _kosdaq_name_to_code = _initialize_kosdaq_data()
    return _kosdaq_name_to_code


# 하위 호환성을 위한 속성 (lazy evaluation)
class _LazyKOSDAQDict:
    """Lazy evaluation을 지원하는 KOSDAQ 딕셔너리 래퍼"""
    def __getitem__(self, key):
        return get_kosdaq_name_to_code()[key]

    def __contains__(self, key):
        return key in get_kosdaq_name_to_code()

    def get(self, key, default=None):
        return get_kosdaq_name_to_code().get(key, default)

    def keys(self):
        return get_kosdaq_name_to_code().keys()

    def values(self):
        return get_kosdaq_name_to_code().values()

    def items(self):
        return get_kosdaq_name_to_code().items()

    def __iter__(self):
        return iter(get_kosdaq_name_to_code())

    def __len__(self):
        return len(get_kosdaq_name_to_code())

    def __repr__(self):
        return repr(get_kosdaq_name_to_code())


# 하위 호환성: 기존 코드가 KOSDAQ_NAME_TO_CODE를 바로 사용할 수 있도록
KOSDAQ_NAME_TO_CODE = _LazyKOSDAQDict()
