from __future__ import annotations

from datetime import date, timedelta
from types import MappingProxyType

from research.crypto_stage_b.contracts import VenueCostLiteral
from research.crypto_stage_b.registry import EXPECTED_RETURN_SHA256, CandidateDefinition
from research.crypto_stage_b.source import DailyBar


def candidate(strategy_id: str) -> CandidateDefinition:
    parameters: dict[str, int | float]
    history_days: int
    if strategy_id == "CR-SPOT-ETR-01":
        history_days = 252
        parameters = {
            "return_history_days": 250,
            "return_tail_quantile": 0.10,
            "close_location_min": 0.70,
            "lower_wick_fraction_min": 0.40,
            "quote_volume_median_days": 30,
            "quote_volume_ratio_min": 1.50,
            "exit_D_plus_days": 3,
            "max_concurrent_positions_per_venue": 5,
        }
    elif strategy_id == "CR-SPOT-TPR-01":
        history_days = 120
        parameters = {
            "long_trend_sma_days": 100,
            "long_trend_slope_lag_days": 20,
            "pullback_sma_days": 20,
            "pullback_integrity_window_days": 5,
            "quote_volume_median_days": 30,
            "quote_volume_ratio_min": 0.75,
            "breakout_exclusion_days": 20,
            "exit_D_plus_days": 10,
            "max_concurrent_positions_per_venue": 5,
        }
    elif strategy_id == "CR-SPOT-CEB-01":
        history_days = 122
        parameters = {
            "normalized_true_range_window_days": 20,
            "compression_reference_count": 100,
            "compression_quantile": 0.25,
            "breakout_lookback_days": 20,
            "close_location_min": 0.80,
            "range_expansion_ratio_min": 1.50,
            "quote_volume_median_days": 30,
            "quote_volume_ratio_min": 2.00,
            "exit_D_plus_days": 7,
            "max_concurrent_positions_per_venue": 5,
        }
    else:  # pragma: no cover - test helper boundary
        raise ValueError(strategy_id)
    return CandidateDefinition(
        strategy_id=strategy_id,
        family_id="test",
        venue_scope="both",
        required_history_days=history_days,
        parameter_values=MappingProxyType(parameters),
        signal_text="source parsed in registry integration test",
        entry_text="next_day_open_utc",
        exit_text="source parsed exit",
        ranking_text="source parsed ranking",
        sizing_text="source parsed sizing",
        ablation_text="source parsed ablation",
        harness_query_text="source parsed harness query",
        raw_contract_text="test fixture only",
        source_return_sha256=EXPECTED_RETURN_SHA256,
        contract_hash=f"test-{strategy_id.lower()}",
    )


def cost(venue: str) -> VenueCostLiteral:
    if venue == "upbit_krw":
        return VenueCostLiteral(
            venue="upbit_krw",
            fee_bp_per_side=5,
            slippage_bp_per_side=10,
            sensitivity_slippage_bp_per_side=30,
        )
    if venue == "binance_usdt_spot":
        return VenueCostLiteral(
            venue="binance_usdt_spot",
            fee_bp_per_side=10,
            slippage_bp_per_side=10,
            sensitivity_slippage_bp_per_side=30,
        )
    raise ValueError(venue)


def etr_bars(
    *,
    venue: str = "upbit_krw",
    symbol: str = "TEST",
    start: date = date(2024, 1, 1),
    quote_volume_on_signal: float = 200.0,
    include_entry: bool = True,
    include_exit: bool = True,
) -> tuple[DailyBar, ...]:
    """Create a single ETR signal on index 251 plus optional D+1/D+3 bars."""
    bars: list[DailyBar] = []
    for index in range(256):
        session = start + timedelta(days=index)
        open_price = 100.0
        high = 101.0
        low = 99.0
        close = 100.0
        quote_volume = 100.0
        if index == 251:
            open_price = 80.0
            high = 90.0
            low = 40.0
            close = 80.0
            quote_volume = quote_volume_on_signal
        elif index == 252:
            open_price = 81.0
            high = 82.0
            low = 80.0
            close = 81.0
        elif index == 255:
            open_price = 90.0
            high = 91.0
            low = 89.0
            close = 90.0
        if (index == 252 and not include_entry) or (index == 255 and not include_exit):
            continue
        bars.append(
            DailyBar(
                venue=venue,
                symbol=symbol,
                session=session,
                open=open_price,
                high=high,
                low=low,
                close=close,
                base_volume=100.0,
                quote_volume=quote_volume,
            )
        )
    return tuple(bars)
