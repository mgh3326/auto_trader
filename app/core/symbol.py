"""
해외주식 심볼 변환 유틸리티

DB 기준 형식: `.` (예: BRK.B)
- Yahoo Finance: `-` (예: BRK-B)
- KIS API: `/` (예: BRK/B)
"""

import re

# KRX 단축코드(국내 종목) 규칙: 숫자 6자리 또는 대문자 ASCII 영문을 포함한 6자리
# (예: 005930, 0256C0). 이 패턴이 단일 원천이다 — 다른 곳에서 새 패턴을 만들지 않고
# 이 상수를 import 한다 (hk 6769, hk 6959, hk 7031). 소문자는 거부한다.
KR_SHORT_CODE = re.compile(r"[0-9A-Z]{6}")


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


def to_upbit_symbol(symbol: str) -> str:
    """Normalize a crypto symbol to the Upbit market-key format.

    Home readers may store a base coin symbol (``BTC``), while the legacy MCP
    contract and quote maps use the explicit fiat market (``KRW-BTC``).
    Existing market-prefixed symbols are preserved.
    """

    normalized = symbol.strip().upper()
    if not normalized or "-" in normalized:
        return normalized
    return f"KRW-{normalized}"


# --- 파생(선물) 계약 심볼 (hk 5946, docs/design/5939-futures-model.md §7) ---------
# DB 정본은 ``<상품 짧은 코드>.<YYMM>`` (예: MF200.2612). 주식의 거래소 접미 `.`
# 과 의미가 다르다. KIS 단축종목코드(``SHTN_PDNO``)와의 대응은 연도 문자 규칙을
# 추측하지 않고 종목마스터 행(이름의 결제월)으로만 정하므로, 이 헬퍼는 계약 심볼의
# 형식만 만들고 읽는다.
_FUTURES_PRODUCT_SHORT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def to_futures_contract_symbol(product_short: str, contract_month: str) -> str:
    """상품 짧은 코드와 결제월(``YYYY-MM``)을 DB 계약 심볼로 변환.

    예: ("MF200", "2026-12") -> "MF200.2612"
    """
    if (
        not 2 <= len(product_short) <= 8
        or not set(product_short) <= _FUTURES_PRODUCT_SHORT_CHARS
        or not product_short[0].isalpha()
    ):
        raise ValueError("상품 짧은 코드 형식이 올바르지 않습니다")
    year, sep, month = contract_month.partition("-")
    if (
        sep != "-"
        or len(year) != 4
        or len(month) != 2
        or not (
            year.isascii() and year.isdigit() and month.isascii() and month.isdigit()
        )
        or not 1 <= int(month) <= 12
    ):
        raise ValueError("결제월 형식이 올바르지 않습니다 (YYYY-MM)")
    return f"{product_short}.{year[2:]}{month}"


def parse_futures_contract_symbol(symbol: str) -> tuple[str, str]:
    """``to_futures_contract_symbol`` 의 역변환: (상품 짧은 코드, ``YYYY-MM``)."""
    product_short, sep, tail = symbol.partition(".")
    if sep != "." or len(tail) != 4 or not (tail.isascii() and tail.isdigit()):
        raise ValueError("파생 계약 심볼 형식이 올바르지 않습니다")
    month = tail[2:]
    if not 1 <= int(month) <= 12:
        raise ValueError("파생 계약 심볼 형식이 올바르지 않습니다")
    contract_month = f"20{tail[:2]}-{month}"
    # 정본 형식 재구성으로 상품 코드 검증을 한 곳에 둔다.
    if to_futures_contract_symbol(product_short, contract_month) != symbol:
        raise ValueError("파생 계약 심볼 형식이 올바르지 않습니다")
    return product_short, contract_month
