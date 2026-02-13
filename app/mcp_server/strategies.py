"""Strategy definitions for `recommend_stocks`."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypedDict

StrategyType = Literal["balanced", "growth", "value", "dividend", "momentum"]


class StrategyConfig(TypedDict):
    description: str
    screen_params: dict[str, Any]
    scoring_weights: dict[str, float]


VALID_STRATEGIES: list[StrategyType] = [
    "balanced",
    "growth",
    "value",
    "dividend",
    "momentum",
]


STRATEGY_CONFIGS: dict[StrategyType, StrategyConfig] = {
    "balanced": {
        "description": "안정적 성장 + 적정 밸류에이션",
        "screen_params": {
            "sort_by": "volume",
            "sort_order": "desc",
            "min_market_cap": 500,
        },
        # 균형 전략: 기술/밸류/모멘텀/유동성을 균등 반영, 배당은 중립.
        "scoring_weights": {
            "rsi_weight": 0.25,
            "valuation_weight": 0.25,
            "momentum_weight": 0.25,
            "volume_weight": 0.25,
            "dividend_weight": 0.0,
        },
    },
    "growth": {
        "description": "고성장 모멘텀 종목",
        "screen_params": {
            "sort_by": "change_rate",
            "sort_order": "desc",
            "min_market_cap": 300,
        },
        # 성장 전략: 모멘텀 + 거래량 중심, 밸류/배당은 낮은 비중.
        "scoring_weights": {
            "rsi_weight": 0.15,
            "valuation_weight": 0.10,
            "momentum_weight": 0.45,
            "volume_weight": 0.30,
            "dividend_weight": 0.0,
        },
    },
    "value": {
        "description": "저평가 가치주",
        "screen_params": {
            "sort_by": "market_cap",
            "sort_order": "desc",
            "max_per": 20.0,
            "max_pbr": 1.5,
            "min_market_cap": 300,
        },
        # 가치 전략: PER/PBR 등 밸류 지표 비중을 가장 크게 둠.
        "scoring_weights": {
            "rsi_weight": 0.30,
            "valuation_weight": 0.40,
            "momentum_weight": 0.10,
            "volume_weight": 0.20,
            "dividend_weight": 0.0,
        },
    },
    "dividend": {
        "description": "고배당 수익률",
        "screen_params": {
            "sort_by": "dividend_yield",
            "sort_order": "desc",
            "min_dividend_yield": 1.5,
            "min_market_cap": 300,
        },
        # 배당 전략: 배당수익률을 핵심으로, 나머지 지표는 보조적으로 사용.
        "scoring_weights": {
            "rsi_weight": 0.15,
            "valuation_weight": 0.20,
            "momentum_weight": 0.10,
            "volume_weight": 0.15,
            "dividend_weight": 0.40,
        },
    },
    "momentum": {
        "description": "단기 모멘텀 트레이딩",
        "screen_params": {
            "sort_by": "volume",
            "sort_order": "desc",
            "min_market_cap": 200,
        },
        # 모멘텀 전략: 단기 가격 추세와 거래량 신호를 최우선으로 반영.
        "scoring_weights": {
            "rsi_weight": 0.20,
            "valuation_weight": 0.05,
            "momentum_weight": 0.50,
            "volume_weight": 0.25,
            "dividend_weight": 0.0,
        },
    },
}


def validate_strategy(strategy: str | None) -> StrategyType:
    """Validate and normalize strategy value."""
    normalized = strategy.lower().strip() if strategy else "balanced"
    if normalized not in VALID_STRATEGIES:
        valid_list = ", ".join(VALID_STRATEGIES)
        raise ValueError(f"Invalid strategy '{strategy}'. Must be one of: {valid_list}")
    return normalized  # type: ignore[return-value]


def get_strategy_config(strategy: StrategyType) -> StrategyConfig:
    """Return a safe copy of strategy config."""
    return deepcopy(STRATEGY_CONFIGS[strategy])


def get_strategy_description(strategy: StrategyType) -> str:
    """Return strategy description text."""
    return STRATEGY_CONFIGS[strategy]["description"]


def get_strategy_screen_params(strategy: StrategyType) -> dict[str, Any]:
    """Return screening parameters for strategy."""
    return deepcopy(STRATEGY_CONFIGS[strategy]["screen_params"])


def get_strategy_scoring_weights(strategy: StrategyType) -> dict[str, float]:
    """Return scoring weights for strategy."""
    return deepcopy(STRATEGY_CONFIGS[strategy]["scoring_weights"])
