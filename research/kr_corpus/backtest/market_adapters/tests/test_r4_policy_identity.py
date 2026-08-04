"""R4/R5: corpus selection is CorpusKind-only; no contract_path public surface."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import schema_contract as _schema_contract_mod
from loader import ManifestEntry, load_shard, sha256_bytes
from market_adapters.common import ContractBackedCorpusAdapter
from market_adapters.crypto import CRYPTO_HOLDOUT_POLICY, CryptoVenueAdapter
from market_adapters.us import USMarketAdapter
from schema_contract import (
    ContractTablePolicyError,
    CorpusKind,
    load_contract,
    required_table_load_policy,
    validate_table_schema,
)

from research.crypto_corpus.policy import UnlabeledParquetError, label_table_for_venue


def _crypto_row(*, frequency: str = "1d") -> dict:
    open_t = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    delta = timedelta(days=1) if frequency == "1d" else timedelta(hours=1)
    return {
        "venue": "upbit_krw",
        "symbol": "KRW-NEW",
        "frequency": frequency,
        "bucket_timezone": "UTC",
        "open_time_utc": open_t,
        "close_time_utc": open_t + delta,
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "base_volume": 1.0,
        "quote_volume": 1.0,
        "trade_count": None,
        "source_candle_date_time_utc": None,
        "source_candle_date_time_kst": None,
        "source_open_time_ms": None,
        "source_close_time_ms": None,
        "source_timestamp_ms": None,
    }


def _crypto_table(*, frequency: str = "1d", labeled: bool = True) -> pa.Table:
    adapter = CryptoVenueAdapter("upbit_krw")
    table = pa.Table.from_pylist(
        [_crypto_row(frequency=frequency)],
        schema=adapter.corpus.arrow_schema_for("ohlcv"),
    )
    if labeled:
        table = label_table_for_venue(table, "upbit_krw")
    else:
        table = table.replace_schema_metadata(None)
    return table


def test_registry_requires_crypto_and_us_and_kr():
    for cid in ("kr-corpus-v1", "crypto-corpus-v1", "us-corpus-v1"):
        assert required_table_load_policy(cid, "ohlcv")
    pol = required_table_load_policy("crypto-corpus-v1", "ohlcv")
    assert pol["require_crypto_parquet_labels"] is True
    assert pol["required_column_values"]["frequency"] == "1d"
    # Not a public export; naive in-place mutation is rejected by MappingProxy.
    assert "CORPUS_TABLE_LOAD_POLICY_BY_ID" not in _schema_contract_mod.__all__
    with pytest.raises(TypeError):
        _schema_contract_mod._CORPUS_TABLE_LOAD_POLICY_BY_ID["x"] = {}  # type: ignore[index]


def test_no_contract_path_on_public_load_apis():
    """B-option: caller cannot supply contract_path on supported load surfaces."""
    assert "contract_path" not in inspect.signature(load_shard).parameters
    assert "contract_path" not in inspect.signature(validate_table_schema).parameters
    assert "contract_path" not in inspect.signature(load_contract).parameters
    fields = getattr(ContractBackedCorpusAdapter, "__dataclass_fields__", {})
    assert "contract_path" not in fields
    assert "corpus" in fields


def test_crypto_kind_refuses_unlabeled_1h_on_all_supported_paths(tmp_path):
    table = _crypto_table(frequency="1h", labeled=False)
    with pytest.raises(UnlabeledParquetError):
        validate_table_schema(table, "ohlcv", corpus=CorpusKind.CRYPTO_V1)

    rel = "ohlcv/upbit_krw/2024/x.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    pq.write_table(table, path)
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=sha256_bytes(path.read_bytes()),
        row_count=1,
        dataset="ohlcv",
        market="upbit_krw",
        year=2024,
    )
    with pytest.raises(UnlabeledParquetError):
        load_shard(
            tmp_path,
            entry,
            corpus=CorpusKind.CRYPTO_V1,
            holdout_policy=CRYPTO_HOLDOUT_POLICY,
        )
    bare = ContractBackedCorpusAdapter(
        corpus=CorpusKind.CRYPTO_V1,
        holdout_policy=CRYPTO_HOLDOUT_POLICY,
    )
    with pytest.raises(UnlabeledParquetError):
        bare.load_shard(tmp_path, entry)
    with pytest.raises(UnlabeledParquetError):
        CryptoVenueAdapter("upbit_krw").load_shard(tmp_path, entry)


def test_crypto_kind_refuses_labeled_hourly():
    table = _crypto_table(frequency="1h", labeled=True)
    with pytest.raises(ContractTablePolicyError):
        validate_table_schema(table, "ohlcv", corpus=CorpusKind.CRYPTO_V1)


def test_cannot_pass_contract_path_kwarg():
    table = _crypto_table(frequency="1d", labeled=True)
    with pytest.raises(TypeError):
        validate_table_schema(
            table,
            "ohlcv",
            contract_path="/tmp/evil.json",  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        ContractBackedCorpusAdapter(
            contract_path="/tmp/evil.json",  # type: ignore[call-arg]
            holdout_policy=CRYPTO_HOLDOUT_POLICY,
        )


def test_normal_three_corpora_load_via_enum(tmp_path):
    # crypto 1d labeled
    table = _crypto_table(frequency="1d", labeled=True)
    validate_table_schema(table, "ohlcv", corpus=CorpusKind.CRYPTO_V1)
    # kr uses default kind via arrow path
    from schema_contract import arrow_schema_for

    kr_schema = arrow_schema_for("ohlcv", corpus=CorpusKind.KR_V1)
    assert "ticker" in kr_schema.names
    # us kind
    us_schema = arrow_schema_for("ohlcv", corpus=CorpusKind.US_V1)
    assert "session_date" in us_schema.names
    assert load_contract(CorpusKind.KR_V1)["corpus_id"] == "kr-corpus-v1"
    assert load_contract(CorpusKind.CRYPTO_V1)["corpus_id"] == "crypto-corpus-v1"
    assert load_contract(CorpusKind.US_V1)["corpus_id"] == "us-corpus-v1"


def test_load_contract_bound_to_kind():
    c = load_contract(CorpusKind.CRYPTO_V1)
    assert c["corpus_id"] == CorpusKind.CRYPTO_V1.value


def test_us_kind_refuses_stripped_metadata(tmp_path):
    from datetime import date

    rows = [
        {
            "symbol": "ABC",
            "session_date": datetime(2024, 7, 3),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1,
        }
    ]
    table = pa.Table.from_pylist(
        rows,
        schema=USMarketAdapter().corpus.arrow_schema_for("ohlcv"),
    ).replace_schema_metadata(None)
    with pytest.raises(ContractTablePolicyError):
        validate_table_schema(table, "ohlcv", corpus=CorpusKind.US_V1)
    _ = date  # silence unused if any
