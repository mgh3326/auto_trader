"""Strategy and account policy definitions for `recommend_stocks`."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypedDict

StrategyType = Literal["balanced", "growth", "value", "dividend", "momentum"]
AccountType = Literal["kis", "toss", "isa", "samsung_pension", "upbit"]


class StrategyConfig(TypedDict):
    description: str
    screen_params: dict[str, Any]
    scoring_weights: dict[str, float]


class AccountConstraint(TypedDict):
    allowed_markets: list[str]
    allowed_asset_types: list[str]


VALID_STRATEGIES: list[StrategyType] = [
    "balanced",
    "growth",
    "value",
    "dividend",
    "momentum",
]

VALID_ACCOUNTS: list[AccountType] = [
    "kis",
    "toss",
    "isa",
    "samsung_pension",
    "upbit",
]


STRATEGY_CONFIGS: dict[StrategyType, StrategyConfig] = {
    "balanced": {
        "description": "안정적 성장 + 적정 밸류에이션",
        "screen_params": {
            "sort_by": "volume",
            "sort_order": "desc",
            "min_market_cap": 500,
        },
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
            "max_per": 15.0,
            "min_market_cap": 1000,
        },
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
            "min_dividend_yield": 2.0,
            "min_market_cap": 500,
        },
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
        "scoring_weights": {
            "rsi_weight": 0.20,
            "valuation_weight": 0.05,
            "momentum_weight": 0.50,
            "volume_weight": 0.25,
            "dividend_weight": 0.0,
        },
    },
}


ACCOUNT_CONSTRAINTS: dict[AccountType, AccountConstraint] = {
    "kis": {
        "allowed_markets": ["kr", "us"],
        "allowed_asset_types": ["stock", "etf"],
    },
    "toss": {
        "allowed_markets": ["kr", "us"],
        "allowed_asset_types": ["stock", "etf"],
    },
    "isa": {
        "allowed_markets": ["kr"],
        "allowed_asset_types": ["etf"],
    },
    "samsung_pension": {
        "allowed_markets": ["kr"],
        "allowed_asset_types": ["etf"],
    },
    "upbit": {
        "allowed_markets": ["crypto"],
        "allowed_asset_types": ["all"],
    },
}


def validate_strategy(strategy: str | None) -> StrategyType:
    """Validate and normalize strategy value."""
    normalized = strategy.lower().strip() if strategy else "balanced"
    if normalized not in VALID_STRATEGIES:
        valid_list = ", ".join(VALID_STRATEGIES)
        raise ValueError(f"Invalid strategy '{strategy}'. Must be one of: {valid_list}")
    return normalized  # type: ignore[return-value]


def validate_account(
    account: str | None,
    market: str,
    asset_type: str | None,
) -> AccountType | None:
    """Validate account policy against target market/asset type."""
    if account is None:
        return None

    normalized = account.lower().strip()
    if normalized not in VALID_ACCOUNTS:
        valid_list = ", ".join(VALID_ACCOUNTS)
        raise ValueError(f"Invalid account '{account}'. Must be one of: {valid_list}")

    constraints = ACCOUNT_CONSTRAINTS[normalized]  # validated above
    allowed_markets = constraints["allowed_markets"]
    allowed_asset_types = constraints["allowed_asset_types"]

    if market not in allowed_markets:
        raise ValueError(
            f"Account '{account}' does not support market '{market}'. "
            f"Allowed markets: {', '.join(allowed_markets)}"
        )

    normalized_asset_type = asset_type.lower().strip() if asset_type else None
    if normalized_asset_type is None:
        # Strict mode: constrained accounts cannot omit asset_type.
        if "all" not in allowed_asset_types and len(allowed_asset_types) == 1:
            raise ValueError(
                f"Account '{account}' requires asset_type "
                f"({', '.join(allowed_asset_types)})."
            )
        return normalized  # type: ignore[return-value]

    if (
        "all" not in allowed_asset_types
        and normalized_asset_type not in allowed_asset_types
    ):
        raise ValueError(
            f"Account '{account}' does not support asset_type '{asset_type}'. "
            f"Allowed types: {', '.join(allowed_asset_types)}"
        )

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
