"""Canonical hash stability and one-field mutation matrix for EvaluationConfig.

Tests ROB-850 AC 8: identical config produces the same hash regardless of
map ordering; changing any currency/source mapping, formula version,
fill/cost rule, benchmark, window, evidence rule or threshold changes the hash.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.paper_evaluation.contracts import (
    CurrencyConversionPolicy,
    EvaluationConfig,
    EvaluationConfigError,
    PartialFillPolicy,
    ViewName,
)
from app.services.paper_evaluation.evaluation_config import compute_config_hash
from tests.services.paper_evaluation.conftest import make_evaluation_config

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stability
# ---------------------------------------------------------------------------


def test_identical_config_produces_same_hash() -> None:
    config = make_evaluation_config()
    assert compute_config_hash(config) == compute_config_hash(config)


def test_hash_is_64_lowercase_hex() -> None:
    config = make_evaluation_config()
    h = compute_config_hash(config)
    assert len(h) == 64
    assert h == h.lower()
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Order independence (AC 8: map ordering must not change hash)
# ---------------------------------------------------------------------------


def test_view_mapping_order_does_not_change_hash() -> None:
    config = make_evaluation_config()
    reversed_views = dict(reversed(list(config.views.items())))
    reversed_equity = dict(reversed(list(config.initial_equity.items())))
    reordered = config.model_copy(
        update={"views": reversed_views, "initial_equity": reversed_equity}
    )
    assert compute_config_hash(config) == compute_config_hash(reordered)


def test_different_key_insertion_order_same_hash() -> None:
    """Simulate building the dict in different insertion orders."""
    base = make_evaluation_config()
    # Build with shadow first
    views_v1 = {
        ViewName.CANONICAL_SHADOW: base.views[ViewName.CANONICAL_SHADOW],
        ViewName.ALPACA_BROKER: base.views[ViewName.ALPACA_BROKER],
        ViewName.BINANCE_BROKER: base.views[ViewName.BINANCE_BROKER],
    }
    equity_v1 = {
        ViewName.CANONICAL_SHADOW: base.initial_equity[ViewName.CANONICAL_SHADOW],
        ViewName.ALPACA_BROKER: base.initial_equity[ViewName.ALPACA_BROKER],
        ViewName.BINANCE_BROKER: base.initial_equity[ViewName.BINANCE_BROKER],
    }
    reordered = base.model_copy(update={"views": views_v1, "initial_equity": equity_v1})
    assert compute_config_hash(base) == compute_config_hash(reordered)


# ---------------------------------------------------------------------------
# One-field mutation matrix (AC 8: each meaningful field change → new hash)
# ---------------------------------------------------------------------------


def test_mdd_target_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(mdd_target_pct=Decimal("26"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_fee_rate_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(fee_rate_bps=Decimal("12"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_spread_bps_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(spread_bps=Decimal("7"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_slippage_bps_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(slippage_bps=Decimal("5"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_risk_free_rate_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(risk_free_rate_pct=Decimal("3"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_initial_equity_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(initial_equity_usdt=Decimal("20000"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_btc_weight_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(
        btc_weight=Decimal("0.6"), eth_weight=Decimal("0.4")
    )
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_min_benchmark_delta_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(min_benchmark_delta_pct=Decimal("0.05"))
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_min_observations_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(min_observations=200)
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_min_fills_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(min_fills=20)
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_min_calendar_days_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(min_calendar_days=14)
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_fill_timing_change_changes_hash() -> None:
    base = make_evaluation_config(fill_timing="next_bar_open")
    mutated = make_evaluation_config(fill_timing="canonical_close")
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_periods_per_year_change_changes_hash() -> None:
    base = make_evaluation_config()
    mutated = make_evaluation_config(periods_per_year=252)
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_partial_fill_policy_change_changes_hash() -> None:
    base = make_evaluation_config(partial_fill_policy=PartialFillPolicy.REJECT_PARTIAL)
    mutated = make_evaluation_config(
        partial_fill_policy=PartialFillPolicy.ACCEPT_PARTIAL_WITH_EVIDENCE,
        partial_fill_ratio=Decimal("0.5"),
    )
    assert compute_config_hash(base) != compute_config_hash(mutated)


def test_partial_fill_ratio_change_changes_hash() -> None:
    base = make_evaluation_config(
        partial_fill_policy=PartialFillPolicy.ACCEPT_PARTIAL_WITH_EVIDENCE,
        partial_fill_ratio=Decimal("0.5"),
    )
    mutated = make_evaluation_config(
        partial_fill_policy=PartialFillPolicy.ACCEPT_PARTIAL_WITH_EVIDENCE,
        partial_fill_ratio=Decimal("0.7"),
    )
    assert compute_config_hash(base) != compute_config_hash(mutated)


# ---------------------------------------------------------------------------
# Currency conversion policy must be in the hash
# ---------------------------------------------------------------------------


def test_currency_conversion_policy_is_none_by_default() -> None:
    config = make_evaluation_config()
    assert config.currency_conversion_policy is CurrencyConversionPolicy.NONE


def test_currency_conversion_policy_is_in_hash_payload() -> None:
    config = make_evaluation_config()
    payload = config.to_hash_payload()
    assert payload["currency_conversion_policy"] == "none"


# ---------------------------------------------------------------------------
# Non-finite / invalid value rejection
# ---------------------------------------------------------------------------


def test_non_finite_initial_equity_rejected() -> None:
    with pytest.raises((ValidationError, EvaluationConfigError)):
        make_evaluation_config(initial_equity_usdt=Decimal("NaN"))


def test_negative_initial_equity_rejected() -> None:
    with pytest.raises((ValidationError, EvaluationConfigError)):
        make_evaluation_config(initial_equity_usdt=Decimal("-100"))


def test_zero_initial_equity_rejected() -> None:
    with pytest.raises((ValidationError, EvaluationConfigError)):
        make_evaluation_config(initial_equity_usdt=Decimal("0"))


def test_negative_fee_rate_rejected() -> None:
    with pytest.raises((ValidationError, EvaluationConfigError)):
        make_evaluation_config(fee_rate_bps=Decimal("-1"))


def test_mdd_target_mismatch_with_thresholds_rejected() -> None:
    """mdd_target_pct must equal promotion_thresholds.max_drawdown_target_pct."""
    from app.services.paper_evaluation.contracts import PromotionThresholds

    config = make_evaluation_config(mdd_target_pct=Decimal("25"))
    dumped = config.model_dump(mode="python")
    dumped["promotion_thresholds"] = PromotionThresholds(
        min_benchmark_delta_pct=Decimal("0.01"),
        max_drawdown_target_pct=Decimal("30"),
    )
    with pytest.raises(EvaluationConfigError):
        EvaluationConfig(**dumped)


def test_config_mappings_are_deeply_immutable() -> None:
    config = make_evaluation_config()
    with pytest.raises(TypeError):
        config.views[ViewName.BINANCE_BROKER] = config.views[ViewName.ALPACA_BROKER]  # type: ignore[index]
    with pytest.raises(TypeError):
        config.initial_equity[ViewName.BINANCE_BROKER] = Decimal("1")  # type: ignore[index]


def test_config_json_roundtrip_preserves_hash_and_immutability() -> None:
    config = make_evaluation_config()
    restored = EvaluationConfig.model_validate_json(config.model_dump_json())
    assert restored.config_hash() == config.config_hash()
    with pytest.raises(TypeError):
        restored.views[ViewName.BINANCE_BROKER] = restored.views[ViewName.ALPACA_BROKER]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Golden byte pinning (detect unintended hash changes)
# ---------------------------------------------------------------------------


def test_golden_hash_pinned() -> None:
    """Pin the canonical hash for the default V1 config.

    If this test fails, the config payload or hash function changed.
    Update this golden value ONLY after review.
    """
    config = make_evaluation_config()
    h = compute_config_hash(config)
    assert h == "53e80a49f62230c663d1285b9d30a6c55d402c5fae02c329bad298a62ce2d6ed"
