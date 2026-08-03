"""Bounded real-corpus mapping smoke + semantic regressions (no strategy claim).

Reads at most 10 bars per market from sealed local artifacts when present.
Skips only when the artifact tree is absent (CI without herdr-artifacts).
Never opens holdout paths for data.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest
from market_adapters.costs import (
    KR_COST_PARAMS_DECLARED,
    KR_COST_WIRED,
    KRCostParamsUnsetError,
    require_kr_cost_model,
)
from market_adapters.crypto import CryptoBar, CryptoVenueAdapter
from market_adapters.us import (
    US_TRADING_VALUE_RESOLUTION,
    USBar,
    USMarketAdapter,
)
from pit import bars_from_table
from schema_contract import CONTRACT_PATH, SchemaMismatchError, validate_table_schema

KR_ORIG = Path(
    "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/runs/"
    "kr-corpus-v1-20260803-1001/dataset/market=KOSPI/year=2024/ticker=000020.parquet"
)
KR_CLAMP = Path(
    "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/derived-views/"
    "clamp-admit-v1/dataset/market=KOSPI/year=2024/ticker=000020.parquet"
)
CRYPTO_UPBIT = Path(
    "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/dataset-labeled/"
    "venue=upbit_krw/year=2024/"
    "KRW-1INCH__1d__20260803T035627753348Z-f20c06c08c1640eabef173fbefb85a78.parquet"
)
US_PART = Path(
    "/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/dataset/market=us/"
    "year=2024/part-00000.parquet"
)

pytestmark = pytest.mark.unit


def _bounded(path: Path, n: int = 10):
    if not path.is_file():
        pytest.skip(f"sealed artifact absent: {path}")
    return pq.ParquetFile(path).read_row_group(0).slice(0, n)


def test_kr_real_original_loads_and_maps_without_imputing_null_value():
    table = _bounded(KR_ORIG, 10)
    validate_table_schema(table, "ohlcv", contract_path=CONTRACT_PATH)
    bars = bars_from_table(table)
    assert len(bars) == 10
    assert bars[0].symbol == "000020"
    assert bars[0].session_date.year == 2024
    assert type(bars[0].open) is int
    # Sealed sample for 000020 has null value — must stay None.
    assert all(b.trading_value is None or type(b.trading_value) is int for b in bars)
    assert bars[0].trading_value is None


def test_kr_real_clamp_view_loads_with_extra_columns():
    table = _bounded(KR_CLAMP, 10)
    validate_table_schema(table, "ohlcv", contract_path=CONTRACT_PATH)
    assert all(
        c in table.column_names
        for c in ("clamped", "admitted", "clamp_delta_high", "clamp_delta_low")
    )
    bars = bars_from_table(table)
    assert len(bars) == 10


def test_crypto_real_upbit_loads_with_units():
    table = _bounded(CRYPTO_UPBIT, 10)
    adapter = CryptoVenueAdapter("upbit_krw")
    view = adapter.view_from_table(table)
    assert view.venue == "upbit_krw"
    assert len(view.bars) == 10
    bar = view.bars[0]
    assert bar.symbol == "KRW-1INCH"
    assert bar.quote_currency == "KRW"
    assert bar.volume == bar.base_volume
    assert bar.session_date.year == 2024
    assert "trading_value" not in CryptoBar.__dataclass_fields__


def test_us_real_loads_without_trading_value():
    # Filter first 10 rows of symbol A from the multi-symbol partition.
    if not US_PART.is_file():
        pytest.skip(f"sealed artifact absent: {US_PART}")
    full = pq.ParquetFile(US_PART).read_row_group(0)
    # Take first 10 rows (symbol A in sealed file).
    table = full.slice(0, 10)
    assert table.column("symbol")[0].as_py() == "A"
    adapter = USMarketAdapter()
    bars = adapter.bars_from_table(table)
    assert len(bars) == 10
    assert bars[0].symbol == "A"
    assert bars[0].session_date.year == 2024
    assert "trading_value" not in USBar.__dataclass_fields__
    assert US_TRADING_VALUE_RESOLUTION == "ABSENT_DECLARED"


def test_us_trading_value_column_is_schema_forbidden():
    if not US_PART.is_file():
        pytest.skip(f"sealed artifact absent: {US_PART}")
    table = pq.ParquetFile(US_PART).read_row_group(0).slice(0, 1)
    import pyarrow as pa

    bad = table.append_column("trading_value", pa.array([1.0]))
    with pytest.raises(SchemaMismatchError) as exc:
        USMarketAdapter().corpus.validate_table_schema(bad, "ohlcv")
    assert "trading_value" in str(exc.value)


def test_kr_cost_wired_without_invented_params():
    assert KR_COST_WIRED is True
    assert KR_COST_PARAMS_DECLARED is False
    with pytest.raises(KRCostParamsUnsetError):
        require_kr_cost_model()
