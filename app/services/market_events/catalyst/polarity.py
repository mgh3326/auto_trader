"""catalyst 카테고리 집합 + impact 극성 매핑 (ROB-408 Slice 1, 순수)."""

from __future__ import annotations

# ROB-408에서 추가된 비실적 촉매 카테고리 (taxonomy.CATEGORIES의 부분집합).
CATALYST_CATEGORIES: frozenset[str] = frozenset(
    {
        "conference",
        "corporate_event",
        "product_launch",
        "policy_regulation",
        "lockup_expiry",
        "index_rebalance",
    }
)

# category → 기본 극성. raw_payload impact_hint가 있으면 그것이 우선(resolve_polarity).
CATEGORY_POLARITY: dict[str, str] = {
    "conference": "positive",
    "product_launch": "positive",
    "index_rebalance": "positive",
    "policy_regulation": "negative",
    "lockup_expiry": "negative",
    "earnings": "neutral",
    "corporate_event": "neutral",
}

_VALID_POLARITY: frozenset[str] = frozenset({"positive", "negative", "neutral"})


def resolve_polarity(category: str, raw_payload: dict | None) -> str:
    """raw_payload['impact_hint'] ∈ {positive,negative,neutral} 우선,
    없으면 CATEGORY_POLARITY, 미지정 category면 'neutral'."""
    if raw_payload:
        hint = raw_payload.get("impact_hint")
        if hint in _VALID_POLARITY:
            return hint
    return CATEGORY_POLARITY.get(category, "neutral")
