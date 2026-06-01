"""결정적 derived 추천 + fail-closed insufficient-data floor (ROB-397).

derive_recommendation 은 저장 입력 + RULE_VERSION 의 순수 함수다 (라이브
호출/랜덤 없음, 입력 동일→출력 동일, 모든 리스트는 안정 정렬). core 입력이
stale/null 이면 확신적 buy/sell 을 금지한다 (ROB-396 증상1).

스코어링 임계값은 app/mcp_server/tooling/shared.py::build_recommendation_for_equity
를 포팅했다 (services → mcp_server import 금지이므로 복제).
"""

from __future__ import annotations

from app.services.symbol_analysis.contract import (
    ConsensusData,
    DerivedBlock,
    FieldBlock,
    PriceData,
    PriceLevel,
    TechnicalData,
)

RULE_VERSION = "symbol_analysis.derived.v1"


def _score_action(
    rsi14: float | None, consensus: ConsensusData | None
) -> tuple[int, int]:
    """(score, max_score). shared.build_recommendation_for_equity 와 동일 임계값."""

    score = 0
    max_score = 0

    if rsi14 is not None:
        max_score += 2
        if rsi14 < 30:
            score += 2
        elif rsi14 < 40:
            score += 1
        elif rsi14 > 70:
            score -= 2
        elif rsi14 > 60:
            score -= 1

    if consensus is not None and consensus.total and consensus.total > 0:
        buy = consensus.buy or 0
        sell = consensus.sell or 0
        max_score += 2
        buy_ratio = buy / consensus.total
        sell_ratio = sell / consensus.total
        if buy_ratio > 0.6:
            score += 2
        elif buy_ratio > 0.4:
            score += 1
        elif sell_ratio > 0.6:
            score -= 2
        elif sell_ratio > 0.4:
            score -= 1

    return score, max_score


def _buy_zones(price: float, tech: TechnicalData | None) -> tuple[PriceLevel, ...]:
    if tech is None:
        return ()
    zones: list[PriceLevel] = []
    if tech.bb_lower is not None and tech.bb_lower < price:
        zones.append(PriceLevel(float(tech.bb_lower), "bollinger_lower", "BB lower band"))
    for s in tech.supports:
        if s < price:
            zones.append(PriceLevel(float(s), "support", f"Support at {s}"))
    return tuple(sorted(zones, key=lambda z: z.price, reverse=True))


def _sell_targets(price: float, tech: TechnicalData | None) -> tuple[PriceLevel, ...]:
    if tech is None:
        return ()
    targets = [
        PriceLevel(float(r), "resistance", f"Resistance at {r}")
        for r in tech.resistances
        if r > price
    ]
    return tuple(sorted(targets, key=lambda z: z.price))


def derive_recommendation(
    *,
    price: FieldBlock[PriceData],
    technicals: FieldBlock[TechnicalData],
    consensus: FieldBlock[ConsensusData],
) -> DerivedBlock:
    # floor 1: 가격 앵커 부재 → unavailable
    if price.value is None:
        return DerivedBlock(
            action="unavailable",
            confidence="low",
            buy_zones=(),
            sell_targets=(),
            stop=None,
            rule_version=RULE_VERSION,
            insufficient_inputs=("price",),
        )

    current = price.value.last
    tech = technicals.value
    cons = consensus.value

    insufficient: list[str] = []
    if tech is None or technicals.is_stale:
        insufficient.append("technicals")
    if cons is None or consensus.is_stale:
        insufficient.append("consensus")

    buy_zones = _buy_zones(current, tech)
    sell_targets = _sell_targets(current, tech)

    # floor 2: core 입력 불완전 → 확신적 buy/sell 금지 (hold, low)
    if insufficient:
        return DerivedBlock(
            action="hold",
            confidence="low",
            buy_zones=buy_zones,
            sell_targets=sell_targets,
            stop=None,
            rule_version=RULE_VERSION,
            insufficient_inputs=tuple(insufficient),
        )

    score, _ = _score_action(tech.rsi14, cons)
    if score >= 2:
        action, confidence = "buy", ("high" if score >= 3 else "medium")
    elif score <= -2:
        action, confidence = "sell", ("high" if score <= -3 else "medium")
    else:
        action, confidence = "hold", "low"

    stop = buy_zones[-1].price if buy_zones else None

    return DerivedBlock(
        action=action,
        confidence=confidence,
        buy_zones=buy_zones,
        sell_targets=sell_targets,
        stop=stop,
        rule_version=RULE_VERSION,
        insufficient_inputs=(),
    )
