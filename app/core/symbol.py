"""
해외주식 심볼 변환 유틸리티

DB 기준 형식: `.` (예: BRK.B)
- Yahoo Finance: `-` (예: BRK-B)
- KIS API: `/` (예: BRK/B)
"""


def to_kis_symbol(symbol: str) -> str:
    """DB 심볼(.)을 KIS API 형식(/)으로 변환

    예: BRK.B -> BRK/B
    """
    return symbol.replace(".", "/")


def to_yahoo_symbol(symbol: str) -> str:
    """DB 심볼(.)을 Yahoo Finance 형식(-)으로 변환

    예: BRK.B -> BRK-B
    """
    return symbol.replace(".", "-")


def to_db_symbol(symbol: str) -> str:
    """외부 심볼을 DB 형식(.)으로 정규화

    예: BRK-B -> BRK.B, BRK/B -> BRK.B
    """
    return symbol.replace("-", ".").replace("/", ".")
