"""ROB-285 — WS reconnect/backoff: exp 1s→60s, jitter ±20%, ≥3 attempts."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.ws_client import (
    compute_backoff_delay,
    is_unhealthy,
)


@pytest.mark.parametrize(
    "attempt,expected_min,expected_max",
    [
        (0, 0.8, 1.2),  # 1s ± 20%
        (1, 1.6, 2.4),  # 2s ± 20%
        (2, 3.2, 4.8),  # 4s ± 20%
        (5, 25.6, 38.4),  # 32s ± 20%
        # Past the cap, base = 60 and jitter is computed against the cap.
        # base + jitter range: [48.0, 72.0].
        (6, 48.0, 72.0),
        (20, 48.0, 72.0),
    ],
)
def test_backoff_delay_bounds(
    attempt: int, expected_min: float, expected_max: float
) -> None:
    # Sample multiple times to verify jitter range.
    samples = [compute_backoff_delay(attempt) for _ in range(100)]
    assert all(expected_min <= s <= expected_max for s in samples), (
        f"Backoff for attempt={attempt} out of [{expected_min}, {expected_max}]: "
        f"min={min(samples)}, max={max(samples)}"
    )


def test_minimum_three_attempts_before_unhealthy() -> None:
    # The state-machine helper requires >=3 consecutive failures
    # before declaring 'unhealthy'.
    assert is_unhealthy(consecutive_failures=0) is False
    assert is_unhealthy(consecutive_failures=1) is False
    assert is_unhealthy(consecutive_failures=2) is False
    assert is_unhealthy(consecutive_failures=3) is True
    assert is_unhealthy(consecutive_failures=10) is True
