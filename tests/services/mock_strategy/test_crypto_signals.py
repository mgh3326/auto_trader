"""Actual research panel/list integration, with synthetic local Parquet files."""

import json
from datetime import date

import pytest

from app.services.mock_strategy.crypto_signals import daily_lists, load_binance_panel
from tests.services.mock_strategy.crypto_fixture import build_fixture

pytestmark = pytest.mark.unit


@pytest.fixture
def panel(tmp_path):
    build_fixture(tmp_path)
    return tmp_path


def test_panel_retains_delisted_history_and_excludes_it_from_today(panel):
    bars = load_binance_panel(panel, date(2023, 12, 31))
    assert "COIN23USDT" in set(bars.symbol)
    assert "__benchmark__" in set(bars.symbol)
    payload = daily_lists(panel, date(2023, 12, 31))
    assert payload["model"] == "relative_volatility_20"
    assert payload["prediction_at"].startswith("2024-01-01T00:05:00")
    assert payload["panel_venue"] == "binance_usdt_spot"
    picks = payload["horizons"]["3"]
    up, down = {n["symbol"] for n in picks["up"]}, {n["symbol"] for n in picks["down"]}
    assert len(up) == len(down) == 10 and not up & down
    assert "COIN23USDT" not in up | down


def test_outside_coverage_is_not_extended_to_today(panel):
    with pytest.raises(ValueError, match="after coverage_end"):
        daily_lists(panel, date(2024, 1, 1))


def test_hash_mismatch_refused_before_using_panel(panel):
    index = json.loads((panel / "universe/candle_files.json").read_text())
    path = panel / index["binance_usdt_spot"][0]["path"]
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_binance_panel(panel, date(2023, 12, 31))
