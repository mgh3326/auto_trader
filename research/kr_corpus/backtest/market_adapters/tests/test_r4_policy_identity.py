"""R4: table_load_policy is corpus-identity derived, not file self-declaration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loader import ManifestEntry, load_shard, sha256_bytes
from market_adapters.crypto import (
    CRYPTO_HOLDOUT_POLICY,
    CRYPTO_SCHEMA_CONTRACT_PATH,
    CryptoVenueAdapter,
)
from schema_contract import (
    CORPUS_TABLE_LOAD_POLICY_BY_ID,
    SCHEMA_ORIGIN,
    ContractTablePolicyError,
    SchemaContractError,
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


def _write_contract_copy(tmp_path: Path, mutator) -> Path:
    raw = json.loads(Path(CRYPTO_SCHEMA_CONTRACT_PATH).read_text(encoding="utf-8"))
    mutator(raw)
    path = tmp_path / "mutated-crypto.schema.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_registry_requires_crypto_and_us_and_kr():
    assert set(CORPUS_TABLE_LOAD_POLICY_BY_ID) == {
        "kr-corpus-v1",
        "crypto-corpus-v1",
        "us-corpus-v1",
    }
    pol = required_table_load_policy("crypto-corpus-v1", "ohlcv")
    assert pol["require_crypto_parquet_labels"] is True
    assert pol["required_column_values"]["frequency"] == "1d"


def test_r4_stripped_policy_key_still_enforces_on_validate(tmp_path):
    """Verifier R4 attack: remove table_load_policy from crypto contract copy."""
    cpath = _write_contract_copy(
        tmp_path, lambda c: c["datasets"]["ohlcv"].pop("table_load_policy", None)
    )
    # File no longer self-declares policy, but corpus_id remains.
    assert "table_load_policy" not in json.loads(cpath.read_text())["datasets"]["ohlcv"]
    unlabeled_1h = _crypto_table(frequency="1h", labeled=False)
    with pytest.raises(UnlabeledParquetError):
        validate_table_schema(unlabeled_1h, "ohlcv", contract_path=cpath)


def test_r4_stripped_policy_key_still_enforces_on_loader(tmp_path):
    cpath = _write_contract_copy(
        tmp_path, lambda c: c["datasets"]["ohlcv"].pop("table_load_policy", None)
    )
    table = _crypto_table(frequency="1h", labeled=False)
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
            contract_path=cpath,
            holdout_policy=CRYPTO_HOLDOUT_POLICY,
        )


def _set_null_policy(c: dict) -> None:
    c["datasets"]["ohlcv"]["table_load_policy"] = None


def _set_empty_policy(c: dict) -> None:
    c["datasets"]["ohlcv"]["table_load_policy"] = {}


def _set_string_policy(c: dict) -> None:
    c["datasets"]["ohlcv"]["table_load_policy"] = "nope"


def _forge_schema_origin(c: dict) -> None:
    c["schema_origin"] = "INFERRED_FROM_LITERALS"


def _drop_corpus_id(c: dict) -> None:
    c.pop("corpus_id", None)


def _unknown_corpus_id(c: dict) -> None:
    c["corpus_id"] = "not-a-real-corpus"


@pytest.mark.parametrize(
    "mutator,exc",
    [
        (_set_null_policy, ContractTablePolicyError),
        (_set_empty_policy, ContractTablePolicyError),
        (_set_string_policy, ContractTablePolicyError),
        (_forge_schema_origin, SchemaContractError),
        (_drop_corpus_id, SchemaContractError),
        (_unknown_corpus_id, SchemaContractError),
    ],
)
def test_r4_policy_and_identity_bypass_variants_fail_closed(tmp_path, mutator, exc):
    cpath = _write_contract_copy(tmp_path, mutator)
    table = _crypto_table(frequency="1h", labeled=False)
    with pytest.raises(exc):
        validate_table_schema(table, "ohlcv", contract_path=cpath)


def test_r4_partial_file_policy_cannot_drop_label_requirement(tmp_path):
    """File declares only frequency; registry still requires labels."""

    def mut(c):
        c["datasets"]["ohlcv"]["table_load_policy"] = {
            "required_column_values": {"frequency": "1d"}
        }

    cpath = _write_contract_copy(tmp_path, mut)
    # labeled 1h: labels OK but frequency wrong → policy error
    with pytest.raises(ContractTablePolicyError):
        validate_table_schema(
            _crypto_table(frequency="1h", labeled=True),
            "ohlcv",
            contract_path=cpath,
        )
    # unlabeled 1d: frequency OK in data but labels required by registry
    with pytest.raises(UnlabeledParquetError):
        validate_table_schema(
            _crypto_table(frequency="1d", labeled=False),
            "ohlcv",
            contract_path=cpath,
        )


def test_r4_normal_sealed_contract_labeled_1d_still_loads(tmp_path):
    adapter = CryptoVenueAdapter("upbit_krw")
    table = _crypto_table(frequency="1d", labeled=True)
    validate_table_schema(table, "ohlcv", contract_path=CRYPTO_SCHEMA_CONTRACT_PATH)
    rel = "ohlcv/upbit_krw/2024/ok.parquet"
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
    view = adapter.load_shard(tmp_path, entry)
    assert len(view.bars) == 1
    assert view.bars[0].frequency == "1d"
    assert view.bars[0].quote_currency == "KRW"


def test_load_contract_requires_corpus_id():
    c = load_contract(CRYPTO_SCHEMA_CONTRACT_PATH)
    assert c["corpus_id"] == "crypto-corpus-v1"
    assert c["schema_origin"] == SCHEMA_ORIGIN
