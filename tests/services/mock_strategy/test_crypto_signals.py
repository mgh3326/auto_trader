"""Actual research panel/list integration, with synthetic local Parquet files."""

import ast
import inspect
import json
from datetime import date
from pathlib import Path

import pytest

from app.services.mock_strategy.crypto_signals import daily_lists, load_binance_panel
from research.crypto_universe import exclusions
from scripts import mock_data_sources
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


def test_producer_and_signal_builder_share_the_crypto_exclusion_definition():
    from app.services.mock_strategy import crypto_signals
    from scripts import mock_data_refresh

    assert (
        mock_data_refresh.crypto_exclusion_reason is exclusions.crypto_exclusion_reason
    )
    assert crypto_signals.crypto_exclusion_reason is exclusions.crypto_exclusion_reason
    assert mock_data_sources.STABLE_BASES is exclusions.STABLE_BASES
    assert (
        mock_data_sources.WRAPPED_DUPLICATE_BASES is exclusions.WRAPPED_DUPLICATE_BASES
    )
    assert mock_data_sources.is_leveraged_token is exclusions.is_leveraged_token

    def module_level_set_definitions(path):
        definitions = []
        for node in ast.parse(Path(path).read_text()).body:
            if isinstance(node, ast.Assign):
                value = node.value
                names = [
                    target.id for target in node.targets if isinstance(target, ast.Name)
                ]
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                names = [node.target.id] if isinstance(node.target, ast.Name) else []
            else:
                continue
            is_set = isinstance(value, ast.Set) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {"set", "frozenset"}
            )
            if is_set:
                definitions.extend(names)
        return definitions

    for module in (mock_data_sources, crypto_signals):
        assert module_level_set_definitions(inspect.getsourcefile(module)) == []
