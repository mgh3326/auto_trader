"""KIS 국내 선물 모의(주간) 주문 경로의 잠금 상수와 닫힌 상품 집합 (hk 5946).

설계 `docs/design/5939-futures-model.md` §1·§8, 운영자 결정
`decision-request/2026-11-21/at-futures-expansion`(Q1–Q12) 및
`decision-request/2026-11-21/at-futures-t3-values`.

🔴 이 값들은 env·CLI·함수 인자로 바꿀 수 없다. 올리려면 새 운영자 결정 + 이 파일과
마이그레이션 CHECK 의 동시 변경이 필요하다(`assert_limits_locked` 가 독립 리터럴과
대조한다). 기존 `kis_mock` 상한(`kis_mock_runner/envelope.py`)과 손실매도 가드는
이 모듈과 무관하며 바뀌지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

ACCOUNT_MODE = "kis_futures_mock"
#: ``review.order_send_intents.account_scope`` 값 — 주식 mock 과 분리된 예약 영역.
RESERVATION_SCOPE = "kis_futures_mock"
BROKER = "kis"

#: 기본 off 이중 게이트의 env 쪽 (정확히 "true"). 호출별 confirm 이 나머지 절반이다.
ENABLED_ENV = "KIS_FUTURES_MOCK_ENABLED"
#: 선물옵션 계좌 8자리 (T1 과 같은 이름). 주식 계좌를 대체하지 않는다.
ACCOUNT_ENV = "KIS_FUTURES_MOCK_ACCOUNT_NO"
#: 선물옵션 계좌 상품코드 (조사 §2.1).
ACCOUNT_PRODUCT_CODE = "03"

#: 모의 호스트 — 송신 전 정확히 일치해야 한다 (kis_mock 과 같은 VTS 호스트).
MOCK_HOST = "https://openapivts.koreainvestment.com:29443"
ORDER_PATH = "/uapi/domestic-futureoption/v1/trading/order"
CANCEL_PATH = "/uapi/domestic-futureoption/v1/trading/order-rvsecncl"
#: KIS 공식 샘플의 모의 주간 TR — 야간 모의 TR 은 존재하지 않는다 (Q7).
TR_PLACE = "VTTO1101U"
TR_CANCEL = "VTTO1103U"

#: 주문처리구분 02=주문전송, 지정가(호가유형·주문구분 01), 조건 없음(0).
ORD_PRCS_DVSN_SEND = "02"
NMPR_TYPE_LIMIT = "01"
ORD_DVSN_LIMIT = "01"
KRX_NMPR_CNDT_NONE = "0"
SLL_BUY_DVSN = {"sell": "01", "buy": "02"}
#: 정정취소구분 02=취소, 전량 취소.
RVSE_CNCL_CANCEL = "02"


@dataclass(frozen=True, slots=True)
class Product:
    """닫힌 상품 집합의 한 항목. 사양 근거는 설계 §8 표의 KRX 상품명세 링크."""

    code: str  # DB 상품 짧은 코드 (심볼 ``MF200.2612`` 의 앞부분)
    multiplier: Decimal  # KRW / 지수 포인트
    tick_size: Decimal  # 포인트
    master_product_type: str  # fo_idx_code_mts.mst 의 상품종류 열
    master_name_pattern: re.Pattern[str]  # 종목마스터 한글종목명 (결제월 캡처)
    master_underlying_name: str
    #: 네트워크 전에 쓰는 단축코드 형식 — 마스터 행으로 다시 확인한다.
    short_code_pattern: re.Pattern[str]


#: 미니 KOSPI200 선물 (2026-11-21 마스터·KRX 명세 확인: 'B' / "미니F 202612" /
#: ``A05612`` = ``A`` + 상품 ``05`` + 연도 끝자리 + 결제월 2자리).
MINI_KOSPI200 = Product(
    code="MF200",
    multiplier=Decimal("50000"),
    tick_size=Decimal("0.02"),
    master_product_type="B",
    master_name_pattern=re.compile(r"^미니F (\d{4})(\d{2})$"),
    master_underlying_name="KOSPI200",
    short_code_pattern=re.compile(r"^A05\d{3}$"),
)
#: 닫힌 집합. 일반 KOSPI200 선물·옵션·스프레드·변동성 등은 목록 밖이다.
CLOSED_PRODUCTS: tuple[Product, ...] = (MINI_KOSPI200,)


@dataclass(frozen=True, slots=True)
class LockedLimits:
    max_order_qty: int = 1
    max_open_positions: int = 1
    max_contract_notional_krw: Decimal = Decimal("50000000")
    daily_loss_limit_krw: Decimal = Decimal("1000000")
    guard_fraction: Decimal = Decimal("0.02")
    limit_only: bool = True
    night_session: bool = False
    options: bool = False
    auto_rollover: bool = False


LOCKED_LIMITS = LockedLimits()

#: ``assert_limits_locked`` 가 대조하는 독립 리터럴 (LOCKED_LIMITS 와 따로 적는다).
_EXPECTED_LOCKED: tuple[object, ...] = (
    1,
    1,
    Decimal("50000000"),
    Decimal("1000000"),
    Decimal("0.02"),
    True,
    False,
    False,
    False,
)
_EXPECTED_PRODUCT = ("MF200", Decimal("50000"), Decimal("0.02"))


class LimitsNotLocked(RuntimeError):
    """잠금 상수가 승인된 값과 다르다 — 주문 경로 시작 전에 거부한다."""


def assert_limits_locked(limits: LockedLimits | None = None) -> LockedLimits:
    """네트워크·DB 사용 전에 호출한다. 승인된 값과 다르면 ``LimitsNotLocked``."""
    current = LOCKED_LIMITS if limits is None else limits
    actual = (
        current.max_order_qty,
        current.max_open_positions,
        current.max_contract_notional_krw,
        current.daily_loss_limit_krw,
        current.guard_fraction,
        current.limit_only,
        current.night_session,
        current.options,
        current.auto_rollover,
    )
    # bool 은 int 의 하위 형식이라 형식까지 함께 비교한다.
    if len(actual) != len(_EXPECTED_LOCKED) or any(
        type(a) is not type(e) or a != e
        for a, e in zip(actual, _EXPECTED_LOCKED, strict=True)
    ):
        raise LimitsNotLocked("선물 모의 잠금 상수가 승인된 값과 다릅니다")
    if (
        CLOSED_PRODUCTS != (MINI_KOSPI200,)
        or (
            MINI_KOSPI200.code,
            MINI_KOSPI200.multiplier,
            MINI_KOSPI200.tick_size,
        )
        != _EXPECTED_PRODUCT
    ):
        raise LimitsNotLocked("선물 모의 상품 집합이 승인된 값과 다릅니다")
    return current
