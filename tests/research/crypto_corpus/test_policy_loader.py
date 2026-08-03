"""Policy-label and fail-closed loader tests for crypto-corpus-v1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.crypto_corpus.artifacts import ArtifactStore
from research.crypto_corpus.labeling import label_existing_exploration_parquet
from research.crypto_corpus.loader import (
    load_labeled_parquet,
    load_labeled_parquet_files,
)
from research.crypto_corpus.policy import (
    CrossVenueReadForbidden,
    HoldoutReadForbidden,
    UnlabeledParquetError,
    UpbitXsecOptInRequired,
    label_table_for_venue,
    policy_from_parquet_metadata,
)


def _table(venue: str) -> pa.Table:
    return pa.table(
        {
            "venue": [venue],
            "symbol": ["KRW-BTC" if venue == "upbit_krw" else "BTCUSDT"],
            "close": [100.0],
        }
    )


def _write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("venue", "label"),
    [
        ("upbit_krw", "SURVIVORSHIP_BIASED"),
        ("binance_usdt_spot", "DELISTED_AVAILABLE_DEGRADED"),
    ],
)
def test_stage_parquet_embeds_per_venue_policy_metadata(tmp_path, venue, label):
    store = ArtifactStore(tmp_path / "artifacts")

    staged = store.stage_parquet(
        _table(venue),
        f"dataset/venue={venue}/year=2024/example.parquet",
        is_holdout=True,
        venue=venue,
    )

    policy = policy_from_parquet_metadata(
        pq.ParquetFile(staged.staging_path).schema_arrow.metadata,
        expected_venue=venue,
    )
    assert policy.survivorship_label == label


def test_loader_rejects_unlabeled_file_with_exception(tmp_path):
    path = tmp_path / "dataset" / "unlabeled.parquet"
    _write_parquet(path, _table("upbit_krw"))

    with pytest.raises(UnlabeledParquetError):
        load_labeled_parquet(path)


def test_upbit_xsec_requires_explicit_survivorship_opt_in(tmp_path):
    path = tmp_path / "dataset" / "upbit.parquet"
    _write_parquet(path, label_table_for_venue(_table("upbit_krw"), "upbit_krw"))

    with pytest.raises(UpbitXsecOptInRequired):
        load_labeled_parquet(path, consumer_intent="xsec")

    loaded = load_labeled_parquet(
        path,
        consumer_intent="xsec",
        allow_upbit_survivorship_biased_xsec=True,
    )
    assert loaded.policy.survivorship_label == "SURVIVORSHIP_BIASED"
    assert loaded.table.num_rows == 1


def test_loader_rejects_cross_venue_read_with_exception(tmp_path):
    upbit = tmp_path / "dataset" / "upbit.parquet"
    binance = tmp_path / "dataset" / "binance.parquet"
    _write_parquet(
        upbit,
        label_table_for_venue(_table("upbit_krw"), "upbit_krw"),
    )
    _write_parquet(
        binance,
        label_table_for_venue(_table("binance_usdt_spot"), "binance_usdt_spot"),
    )

    with pytest.raises(CrossVenueReadForbidden):
        load_labeled_parquet_files((upbit, binance))


def test_loader_rejects_holdout_path_before_opening_file(tmp_path):
    missing_holdout_path = tmp_path / "holdout" / "not-opened.parquet"

    with pytest.raises(HoldoutReadForbidden):
        load_labeled_parquet(missing_holdout_path)


def test_label_migration_preserves_source_values_and_never_scans_holdout(tmp_path):
    root = tmp_path / "artifacts"
    upbit_source = root / "dataset" / "venue=upbit_krw" / "year=2024" / "u.parquet"
    binance_source = (
        root / "dataset" / "venue=binance_usdt_spot" / "year=2024" / "b.parquet"
    )
    _write_parquet(upbit_source, _table("upbit_krw"))
    _write_parquet(binance_source, _table("binance_usdt_spot"))
    source_hashes = {
        upbit_source: _sha256(upbit_source),
        binance_source: _sha256(binance_source),
    }

    holdout_sentinel = root / "holdout" / "must-not-be-read.parquet"
    holdout_sentinel.parent.mkdir(parents=True, exist_ok=True)
    holdout_sentinel.write_bytes(b"not a parquet file")

    result = label_existing_exploration_parquet(root)

    assert result.file_count == 2
    assert result.row_count == 2
    assert {record.venue for record in result.records} == {
        "upbit_krw",
        "binance_usdt_spot",
    }
    assert all(record.values_equivalent for record in result.records)
    assert {path: _sha256(path) for path in source_hashes} == source_hashes
    assert upbit_source.exists()
    assert binance_source.exists()

    for record in result.records:
        source = root / record.source_relative_path
        labeled = root / record.labeled_relative_path
        assert (
            pq.ParquetFile(source)
            .read()
            .equals(
                pq.ParquetFile(labeled).read(),
                check_metadata=False,
            )
        )
        policy_from_parquet_metadata(pq.ParquetFile(labeled).schema_arrow.metadata)

    receipt = root / result.receipt_relative_path
    payload = json.loads(receipt.read_text())
    assert payload["holdout_read_operations"] == 0
    assert payload["data_values_changed"] is False


def test_label_migration_refuses_to_overwrite_existing_labeled_tree(tmp_path):
    root = tmp_path / "artifacts"
    source = root / "dataset" / "venue=upbit_krw" / "year=2024" / "u.parquet"
    _write_parquet(source, _table("upbit_krw"))

    label_existing_exploration_parquet(root)

    with pytest.raises(FileExistsError, match="will not be overwritten"):
        label_existing_exploration_parquet(root)
