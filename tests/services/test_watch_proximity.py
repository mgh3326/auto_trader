from __future__ import annotations

import importlib

import pytest

from app.services.watch_proximity import (
    WatchProximityBand,
    build_proximity_dedupe_key,
    compute_price_proximity,
    format_proximity_message,
)


@pytest.mark.parametrize(
    ("current", "expected_band", "expected_triggered"),
    [
        (98.50, "outside", False),
        (99.10, "within_1_pct", False),
        (99.60, "within_0_5_pct", False),
        (100.00, "hit", True),
        (101.00, "hit", True),
    ],
)
def test_price_above_proximity_bands(
    current: float,
    expected_band: WatchProximityBand,
    expected_triggered: bool,
) -> None:
    result = compute_price_proximity(
        market="kr",
        target_kind="asset",
        symbol="005930",
        condition_type="price_above",
        threshold=100.0,
        current=current,
    )

    assert result.distance_abs == pytest.approx(100.0 - current)
    assert result.distance_pct == pytest.approx(
        0.0 if expected_triggered else abs(100.0 - current) / 100.0 * 100
    )
    assert result.band == expected_band
    assert result.triggered is expected_triggered


@pytest.mark.parametrize(
    ("current", "expected_band", "expected_triggered"),
    [
        (101.50, "outside", False),
        (100.90, "within_1_pct", False),
        (100.40, "within_0_5_pct", False),
        (100.00, "hit", True),
        (99.00, "hit", True),
    ],
)
def test_price_below_proximity_bands(
    current: float,
    expected_band: WatchProximityBand,
    expected_triggered: bool,
) -> None:
    result = compute_price_proximity(
        market="us",
        target_kind="asset",
        symbol="AMZN",
        condition_type="price_below",
        threshold=100.0,
        current=current,
    )

    assert result.distance_abs == pytest.approx(current - 100.0)
    assert result.distance_pct == pytest.approx(
        0.0 if expected_triggered else abs(current - 100.0) / 100.0 * 100
    )
    assert result.band == expected_band
    assert result.triggered is expected_triggered


def test_proximity_dedupe_key_is_stable_and_band_specific() -> None:
    result = compute_price_proximity(
        market="crypto",
        target_kind="asset",
        symbol="btc",
        condition_type="price_above",
        threshold=100.0,
        current=99.6,
    )

    assert result.dedupe_key == (
        "watch-proximity:crypto:asset:BTC:price_above:100:within_0_5_pct"
    )
    assert build_proximity_dedupe_key(result, "within_1_pct") == (
        "watch-proximity:crypto:asset:BTC:price_above:100:within_1_pct"
    )


def test_format_proximity_message_includes_final_approval_requirement() -> None:
    result = compute_price_proximity(
        market="kr",
        target_kind="index",
        symbol="KOSPI",
        condition_type="price_below",
        threshold=3000.0,
        current=3012.0,
    )

    message = format_proximity_message([result])

    assert "Watch proximity alerts (kr)" in message
    assert "KOSPI price_below" in message
    assert "current=3012.0000" in message
    assert "threshold=3000.0000" in message
    assert "distance_abs=12.0000" in message
    assert "distance_pct=0.4000%" in message
    assert "band=within_0_5_pct" in message
    assert (
        "This is an informational alert only; any order requires final user approval."
        in message
    )


def test_watch_proximity_module_has_no_order_or_watch_registration_imports() -> None:
    module = importlib.import_module("app.services.watch_proximity")

    forbidden = {
        "kis_trading_service",
        "order_execution",
        "orders_registration",
        "watch_alerts_registration",
        "paper_order_handler",
    }
    assert forbidden.isdisjoint(set(module.__dict__))
