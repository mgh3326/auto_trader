"""Shared fixtures for ROB-850 paper evaluation tests."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from app.services.paper_evaluation.contracts import (
    AnnualizationRules,
    BenchmarkWeights,
    CurrencyConversionPolicy,
    EvaluationConfig,
    FillCostPolicy,
    MarkFillTiming,
    MinimumEvidence,
    MissingDataPolicy,
    PartialFillPolicy,
    PromotionThresholds,
    ViewCurrency,
    ViewMapping,
    ViewName,
    ViewSource,
)


def stable_hash(label: str) -> str:
    """Deterministic SHA-256 for test identity values."""
    return hashlib.sha256(label.encode()).hexdigest()


def make_view_mapping(view_name: ViewName) -> ViewMapping:
    """Create the canonical V1 view mapping for ``view_name``."""
    if view_name is ViewName.BINANCE_BROKER:
        return ViewMapping(
            view_name=view_name,
            currency=ViewCurrency.USDT,
            source=ViewSource.BINANCE_DEMO_LEDGER,
            symbols=("BTCUSDT", "ETHUSDT"),
            benchmark_symbols=("BTCUSDT", "ETHUSDT"),
        )
    if view_name is ViewName.ALPACA_BROKER:
        return ViewMapping(
            view_name=view_name,
            currency=ViewCurrency.USD,
            source=ViewSource.ALPACA_PAPER_LEDGER,
            symbols=("BTC/USD", "ETH/USD"),
            benchmark_symbols=("BTC/USD", "ETH/USD"),
        )
    return ViewMapping(
        view_name=view_name,
        currency=ViewCurrency.USDT,
        source=ViewSource.CANONICAL_MARKET_SNAPSHOT,
        symbols=("BTCUSDT", "ETHUSDT"),
        benchmark_symbols=("BTCUSDT", "ETHUSDT"),
    )


def make_evaluation_config(
    *,
    mdd_target_pct: Decimal = Decimal("25"),
    min_benchmark_delta_pct: Decimal = Decimal("0.01"),
    fee_rate_bps: Decimal = Decimal("10"),
    spread_bps: Decimal = Decimal("5"),
    slippage_bps: Decimal = Decimal("3"),
    risk_free_rate_pct: Decimal = Decimal("2"),
    shadow_soak_days: int = 7,
    paper_promotion_days: int = 60,
    initial_equity_usdt: Decimal = Decimal("10000"),
    initial_equity_usd: Decimal = Decimal("10000"),
    btc_weight: Decimal = Decimal("0.5"),
    eth_weight: Decimal = Decimal("0.5"),
    min_observations: int = 100,
    min_fills: int = 10,
    min_calendar_days: int = 7,
    partial_fill_policy: PartialFillPolicy = PartialFillPolicy.REJECT_PARTIAL,
    partial_fill_ratio: Decimal = Decimal("0"),
    fill_timing: str = "next_bar_open",
    periods_per_year: int = 525600,
) -> EvaluationConfig:
    """Build a valid V1 EvaluationConfig with overridable fields."""
    return EvaluationConfig(
        views={
            ViewName.BINANCE_BROKER: make_view_mapping(ViewName.BINANCE_BROKER),
            ViewName.ALPACA_BROKER: make_view_mapping(ViewName.ALPACA_BROKER),
            ViewName.CANONICAL_SHADOW: make_view_mapping(ViewName.CANONICAL_SHADOW),
        },
        initial_equity={
            ViewName.BINANCE_BROKER: initial_equity_usdt,
            ViewName.ALPACA_BROKER: initial_equity_usd,
            ViewName.CANONICAL_SHADOW: initial_equity_usdt,
        },
        canonical_snapshot_source="binance_public_spot",
        canonical_snapshot_schema="canonical_market_snapshot.v1",
        mark_fill_timing=MarkFillTiming(
            mark_timing="canonical_close",
            fill_timing=fill_timing,
        ),
        fill_cost_policy=FillCostPolicy(
            fee_rate_bps=fee_rate_bps,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            partial_fill_policy=partial_fill_policy,
            partial_fill_ratio=partial_fill_ratio,
        ),
        annualization=AnnualizationRules(
            periods_per_year=periods_per_year,
            risk_free_rate_pct=risk_free_rate_pct,
        ),
        benchmark_weights=BenchmarkWeights(
            btc_weight=btc_weight,
            eth_weight=eth_weight,
        ),
        minimum_evidence=MinimumEvidence(
            min_observations=min_observations,
            min_fills=min_fills,
            min_calendar_days=min_calendar_days,
        ),
        promotion_thresholds=PromotionThresholds(
            min_benchmark_delta_pct=min_benchmark_delta_pct,
            max_drawdown_target_pct=mdd_target_pct,
        ),
        mdd_target_pct=mdd_target_pct,
        currency_conversion_policy=CurrencyConversionPolicy.NONE,
        missing_data_policy=MissingDataPolicy.FAIL_CLOSE,
    )
