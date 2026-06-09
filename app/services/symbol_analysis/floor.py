"""fail-closed insufficient-data floor 헬퍼 (ROB-396, ROB-397 정책 재사용).

core 입력(price/rsi14/consensus)이 부족하면 확신적 buy/sell 을 금지한다.
price 부재면 unavailable, 그 외 부족이면 hold 로 내린다.
"""

from __future__ import annotations

# floor 가 검사하는 core 입력 순서 (insufficient_inputs 출력 순서 고정 → 결정적).
_CORE_FIELDS: tuple[str, ...] = ("price", "rsi14", "consensus")


def insufficient_inputs(
    *, price_present: bool, rsi_present: bool, consensus_present: bool
) -> list[str]:
    """부재한 core 입력 이름 리스트 (고정 순서)."""

    present = {
        "price": price_present,
        "rsi14": rsi_present,
        "consensus": consensus_present,
    }
    return [field for field in _CORE_FIELDS if not present[field]]


def floored_action(
    action: str, confidence: str, *, insufficient: list[str]
) -> tuple[str, str]:
    """(action, confidence). price 부재→(unavailable, low); 그 외 부족→(hold, low);
    부족 없으면 입력 그대로 통과."""

    if "price" in insufficient:
        return "unavailable", "low"
    if insufficient:
        return "hold", "low"
    return action, confidence
