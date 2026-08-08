"""Fixed trust labels — verbatim from the ROB-1230 design doc, §0.

Source of record: ~/work/herdr-inbox/rob1230-b0-policy-table-design-v1.md §0
(quoted verbatim, wrapped lines joined with spaces). Every policy_table.v1
artifact carries all three, in this order, in its header. Never edit the
wording here without updating the design doc first — the acceptance gate
checks for exact-string presence (3/3).

US (P-2-US / B0-X contract §1) additionally stamps
``CROSS_MARKET_TRANSFER_UNVALIDATED`` — B0 was calibrated on KR scope; a US
port is double-unvalidated. Crypto/KR adapters keep the original 3-label
tuple; only the US adapter uses ``US_TRUST_LABELS``.
"""

from __future__ import annotations

TRUST_LABELS: tuple[str, str, str] = (
    "B0_UNVALIDATED — 이 표는 운영자 현행 규칙(B0)의 계산기이지 검증된 전략이 아니다.",
    (
        "SELL_SIDE_MODEL_MISMATCH — C2P calibration 실측: B0 매도 모형(50/50 저항 사다리·"
        "전량청산 부재)은 운영자 실제 2025 행동(전량매도 중앙 1.0·연 ~68 cycle)을 재현하지 "
        "못했다. → 매도측 제안값은 참고 등급. 매수측 산술(지지·사다리·물타기 금액)은 "
        "불일치와 무관하게 유효."
    ),
    (
        "FIDELITY_INCONCLUSIVE_COVERAGE — 일봉 체결 가정의 과거 소급 검증 불가(F1). "
        "체결 충실도 증거는 전향 사용에서만 축적된다."
    ),
)

# B0-X experiment contract §1 + design inheritance for US port.
CROSS_MARKET_TRANSFER_UNVALIDATED = (
    "CROSS_MARKET_TRANSFER_UNVALIDATED — B0 는 KR 스코프 계량 — US 이식은 이중 미검증."
)

US_TRUST_LABELS: tuple[str, str, str, str] = (
    *TRUST_LABELS,
    CROSS_MARKET_TRANSFER_UNVALIDATED,
)

__all__ = [
    "TRUST_LABELS",
    "CROSS_MARKET_TRANSFER_UNVALIDATED",
    "US_TRUST_LABELS",
]
