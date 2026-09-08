"""The port in ``levels.py`` equals the production arithmetic, value for value.

The study never imports ``app`` — doing so drags in the broker clients and
therefore ``app.core.config.settings``, which needs live credentials.  This
test does import them, under throw-away dummy settings, purely to prove the
port is faithful.  It skips (rather than fails) where that import is not
possible, because the study's own correctness does not depend on it.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from research.underwater_spike_trim_study import levels as port

_DUMMY_ENV = {
    "kis_app_key": "dummy",
    "kis_app_secret": "dummy",
    "opendart_api_key": "dummy",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "upbit_access_key": "dummy",
    "upbit_secret_key": "dummy",
    "SECRET_KEY": "Abcdef0123456789Abcdef0123456789Abcdef01",
}


@pytest.fixture(scope="module")
def repo():
    for key, value in _DUMMY_ENV.items():
        os.environ.setdefault(key, value)
    try:
        import app.mcp_server.tooling.market_data_indicators as module
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"production indicators not importable here: {exc}")
    return module


def _frame(seed: int, rows: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.03, rows)))
    spread = np.abs(rng.normal(0, 0.02, rows)) * close
    high = close + spread
    low = np.maximum(close - spread, close * 0.5)
    return pd.DataFrame(
        {
            "high": high,
            "low": low,
            "close": close,
            "open": close,
            "volume": rng.integers(1, 10_000, rows).astype(float),
        }
    )


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_rsi_last_value_matches(repo, seed: int):
    frame = _frame(seed)
    expected = repo._calculate_rsi(frame["close"])[str(repo.DEFAULT_RSI_PERIOD)]
    got = port.rsi_series(frame["close"]).iloc[-1]
    assert round(float(got), 2) == pytest.approx(expected)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_bollinger_matches(repo, seed: int):
    frame = _frame(seed)
    assert port.calculate_bollinger(frame["close"]) == repo._calculate_bollinger(
        frame["close"]
    )


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_fibonacci_levels_match_at_production_rounding(repo, seed: int):
    frame = _frame(seed)
    price = float(frame["close"].iloc[-1])
    expected = repo._calculate_fibonacci(frame, price)
    got = port.calculate_fibonacci(frame, price, price_decimals=2)
    assert got["levels"] == expected["levels"]
    assert got["trend"] == expected["trend"]
    assert got["swing_high"]["price"] == expected["swing_high"]["price"]
    assert got["swing_low"]["price"] == expected["swing_low"]["price"]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_volume_profile_poc_and_value_area_match(repo, seed: int):
    frame = _frame(seed)
    expected = repo._calculate_volume_profile(frame, bins=20)
    got = port.calculate_volume_profile(frame, bins=20)
    assert got["poc"]["price"] == expected["poc"]["price"]
    assert got["value_area"]["high"] == expected["value_area"]["high"]
    assert got["value_area"]["low"] == expected["value_area"]["low"]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_clustering_and_split_match_end_to_end(repo, seed: int):
    """Rebuild the production handler's pure core and compare cluster for cluster."""
    frame = _frame(seed)
    price = round(float(frame["close"].iloc[-1]), 2)

    fib = repo._calculate_fibonacci(frame, price)
    volume_profile = repo._calculate_volume_profile(frame, bins=20)
    bollinger = repo._calculate_bollinger(frame["close"])
    expected_inputs: list[tuple[float, str]] = [
        (float(value), repo._format_fibonacci_source(str(key)))
        for key, value in fib["levels"].items()
    ]
    expected_inputs.append((float(volume_profile["poc"]["price"]), "volume_poc"))
    expected_inputs.append(
        (float(volume_profile["value_area"]["high"]), "volume_value_area_high")
    )
    expected_inputs.append(
        (float(volume_profile["value_area"]["low"]), "volume_value_area_low")
    )
    for key, source in (
        ("upper", "bb_upper"),
        ("middle", "bb_middle"),
        ("lower", "bb_lower"),
    ):
        expected_inputs.append((float(bollinger[key]), source))
    expected_supports, expected_resistances = repo._split_support_resistance_levels(
        repo._cluster_price_levels(expected_inputs, tolerance_pct=0.02), price
    )

    view = port.compute_levels(frame, current_price=price, price_decimals=2)
    assert [level["price"] for level in view.supports] == [
        level["price"] for level in expected_supports
    ]
    assert [level["strength"] for level in view.supports] == [
        level["strength"] for level in expected_supports
    ]
    assert [level["price"] for level in view.resistances] == [
        level["price"] for level in expected_resistances
    ]
    assert [level["sources"] for level in view.resistances] == [
        level["sources"] for level in expected_resistances
    ]
