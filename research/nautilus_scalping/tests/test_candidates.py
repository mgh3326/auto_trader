# tests/test_candidates.py
from __future__ import annotations

from decimal import Decimal

from candidates import REGISTRY, get_candidate


def test_registry_has_required_members() -> None:
    assert "micro_breakout" in REGISTRY        # baseline, not silently treated as viable
    assert "meanrev_zscore_fade" in REGISTRY    # the new non-breakout candidate
    assert "random_entry" in REGISTRY           # honest control


def test_candidate_metadata_shape() -> None:
    c = get_candidate("meanrev_zscore_fade")
    assert c.hypothesis == "mean_reversion"
    assert callable(c.pure_signal)
    assert isinstance(c.default_params, dict)


def test_pure_signal_is_deterministic_via_registry() -> None:
    from app.services.brokers.binance.demo_scalping.signal import Candle
    c = get_candidate("meanrev_zscore_fade")
    candles = [
        Candle(open_time_ms=i * 60_000, open=Decimal("100"), high=Decimal("100.5"),
               low=Decimal("99.5"), close=Decimal("100"), close_time_ms=i * 60_000)
        for i in range(19)
    ] + [Candle(open_time_ms=19 * 60_000, open=Decimal("97"), high=Decimal("100"),
                low=Decimal("96.5"), close=Decimal("97"), close_time_ms=19 * 60_000)]
    cfg = c.config_factory(c.default_params)
    assert c.pure_signal(candles, cfg) == c.pure_signal(candles, cfg)


def test_unknown_candidate_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        get_candidate("does_not_exist")
