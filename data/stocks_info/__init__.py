# app/data/stocks_info/__init__.py
"""KIS 종목·섹터 코드 마스터 모음."""

from .kis_kospi_code_mst import KOSPI_NAME_TO_CODE  # dict[str,str]
from .kis_kosdaq_code_mst import KOSDAQ_NAME_TO_CODE

# from .sector_code import SECTOR_CODE_DICT

KRX_NAME_TO_CODE: dict[str, str] = {
    **KOSPI_NAME_TO_CODE,
    **KOSDAQ_NAME_TO_CODE,
    # **KONEX_NAME_TO_CODE,  # 필요 시
}

__all__ = [
    "KOSPI_NAME_TO_CODE",
    "KOSDAQ_NAME_TO_CODE",
    "KRX_NAME_TO_CODE"
]
