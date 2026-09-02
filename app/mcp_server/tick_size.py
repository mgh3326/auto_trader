"""KRX (Korean Exchange) tick size adjustment for Korean securities.

This module provides tick size adjustment logic following KRX's standard
tick size tables, which are required for placing limit orders on Korean
stocks and ETPs.

Based on KRX market rules (2023+):
- Buy orders: Round DOWN (floor) to nearest tick
- Sell orders: Round UP (ceil) to nearest tick

References:
- KRX 유가증권시장 매매거래제도 일반: https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100.jsp
- KRX 코스닥시장 매매거래제도 일반: https://regulation.krx.co.kr/contents/RGL/03/03020100/RGL03020100.jsp
- **유가증권시장 업무규정 시행세칙 제32조(호가단위) 제2항 제2호** (1차 규정 원문 —
  ETF="상장지수집합투자기구 집합투자증권", ETN="상장지수증권"을 같은 호에서 함께
  규정하며, 2,000원 미만 1원 / 2,000원 이상 5원 티어의 근거 조문). 부칙 <제2181호,
  2023. 10. 17.>에 따라 **2023년 12월 11일 시행**. 세칙은 rule.krx.co.kr 법무포털의
  세션 렌더링 뷰어로만 열람 가능해 영구 링크가 없다 — 도달 경로: KRX 법무포털
  (https://rule.krx.co.kr) 홈 검색창에 "호가가격단위" 검색 → 결과 목록에서
  "유가증권시장 업무규정 시행세칙"(세칙, bookid=330001869) 클릭 → 팝업 뷰어에서
  "제32조(호가단위)" 확인. 코스닥시장 업무규정 시행세칙(bookid=210223538) 제18조에는
  ETF/ETN 항목 자체가 없음 — ETF·ETN은 유가증권시장에만 상장되므로 코스닥 대응
  조항은 존재하지 않는다(N/A, 아래 표는 유가증권시장 조문 기준).
  (참고: 아래 두 개요 페이지는 investor-facing 설명 페이지로 "호가가격단위 5원"
  단일값만 표기하고 2,000원 구간 반영이 없는 stale 콘텐츠라 1차 인용에서 제외함 —
  https://regulation.krx.co.kr/contents/RGL/03/03060101/RGL03060101.jsp (ETF),
  https://regulation.krx.co.kr/contents/RGL/03/03060201/RGL03060201.jsp (ETN))

KRX Tick Size Table (KRW, 2023-12-11+):
| Price Range       | Tick Size |
|-------------------|-----------|
| ~2,000            | 1         |
| 2,000-5,000       | 5         |
| 5,000-20,000      | 10        |
| 20,000-50,000     | 50        |
| 50,000-200,000    | 100       |
| 200,000-500,000   | 500       |
| 500,000~          | 1,000     |

KRX ETF/ETN Tick Size Table (KRW, 유가증권시장 업무규정 시행세칙 제32조②2호,
2023-12-11 시행):
| Price Range       | Tick Size |
|-------------------|-----------|
| ~2,000            | 1         |
| 2,000~            | 5         |

The caller supplies ``security_type`` from the trusted KR symbol-universe
record.  Only the explicit ``ETF`` and ``ETN`` values select the ETP table;
missing or unrecognised values deliberately retain the stock table.  Note:
주식워런트증권(ELW, 제32조②3호)은 가격 구간과 무관하게 항상 5원 고정인 별도
카테고리로, ETF/ETN 표에도 일반 주권 표에도 속하지 않는다 — 이 코드베이스는
현재 ``security_type="ELW"``를 채우는 경로가 없어 영향은 없지만, 향후 ELW를
다루게 될 경우 ``_ETP_SECURITY_TYPES``에 포함시키지 말고 별도 flat-5원 처리가
필요하다.
"""

import math

_ETP_SECURITY_TYPES = frozenset({"ETF", "ETN"})


def is_krx_etp_security_type(security_type: str | None) -> bool:
    """Return whether a trusted universe classification is a KRX ETF or ETN.

    This deliberately has no symbol-list or proposer-payload fallback.  A
    missing or unrecognised classification is not evidence that an instrument
    is an ETP, so callers retain the established stock tick table.
    """
    return (
        isinstance(security_type, str)
        and security_type.strip().upper() in _ETP_SECURITY_TYPES
    )


def get_tick_size_kr(price: float, security_type: str | None = None) -> int:
    """Return the KRX tick size for a price and trusted security classification.

    Args:
        price: Security price in KRW
        security_type: ``kr_symbol_universe.security_type``. Explicit ETF/ETN
            values use the KRX ETP table; missing/unknown values use the
            established stock table conservatively.

    Returns:
        Tick size in KRW
    """
    if is_krx_etp_security_type(security_type):
        return 1 if price < 2000 else 5
    if price < 2000:
        return 1
    elif price < 5000:
        return 5
    elif price < 20000:
        return 10
    elif price < 50000:
        return 50
    elif price < 200000:
        return 100
    elif price < 500000:
        return 500
    else:
        return 1000


def _get_tick_size(price: float, security_type: str | None = None) -> int:
    """Return the tick size for a given price based on KRX rules.

    DEPRECATED: Use get_tick_size_kr() instead.

    Args:
        price: Stock price in KRW

    Returns:
        Tick size in KRW
    """
    return get_tick_size_kr(price, security_type)


def adjust_tick_size_kr(
    price: float, side: str = "buy", security_type: str | None = None
) -> int:
    """Adjust price to KRX tick size rules.

    For Korean stocks (equity_kr), prices must align with tick sizes:
    - Buy orders: Round DOWN (floor) - lower price for better execution
    - Sell orders: Round UP (ceil) - higher price for better execution

    Args:
        price: Price to adjust in KRW
        side: Order side ("buy" or "sell")
        security_type: Trusted universe classification. Explicit ETF/ETN values
            use the KRX ETP table; missing/unknown values retain the stock table.

    Returns:
        Adjusted price in KRW (integer)

    Examples:
        >>> adjust_tick_size_kr(327272, "buy")
        327000
        >>> adjust_tick_size_kr(327272, "sell")
        327500
        >>> adjust_tick_size_kr(1098000, "buy")
        1098000
        >>> adjust_tick_size_kr(15723, "buy")
        15720
    """
    if price < 0:
        raise ValueError(f"Price must be non-negative, got {price}")

    tick_size = _get_tick_size(price, security_type)

    # Normalize to tick size boundaries
    if side == "buy":
        # Round DOWN (floor) - better for buy orders (lower price)
        adjusted = math.floor(price / tick_size) * tick_size
    elif side == "sell":
        # Round UP (ceil) - better for sell orders (higher price)
        adjusted = math.ceil(price / tick_size) * tick_size
    else:
        raise ValueError(f"side must be 'buy' or 'sell', got '{side}'")

    # Ensure minimum price of 1 KRW
    adjusted = max(1, int(adjusted))

    return adjusted
