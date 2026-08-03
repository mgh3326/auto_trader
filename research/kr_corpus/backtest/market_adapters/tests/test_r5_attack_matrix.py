"""R5 attack matrix: all prior bypass classes against CorpusKind-only API."""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from loader import ManifestEntry, load_shard, sha256_bytes
from market_adapters.common import ContractBackedCorpusAdapter
from market_adapters.crypto import (
    CRYPTO_HOLDOUT_POLICY,
    CryptoVenueAdapter,
)
from schema_contract import (
    CorpusKind,
)

H1 = Path(
    "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/dataset-labeled/"
    "venue=upbit_krw/year=2024/"
    "KRW-MINA__1h__20260803T050258445029Z-c9e1c32e429b47d4b120bb6f88d5694b.parquet"
)
D1 = Path(
    "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/dataset-labeled/"
    "venue=upbit_krw/year=2024/"
    "KRW-1INCH__1d__20260803T035627753348Z-f20c06c08c1640eabef173fbefb85a78.parquet"
)


def _probe(fn) -> str:
    try:
        r = fn()
        if hasattr(r, "num_rows"):
            return f"DATA_RETURNED rows={r.num_rows}"
        if hasattr(r, "bars"):
            return f"DATA_RETURNED bars={len(r.bars)}"
        if isinstance(r, list):
            return f"DATA_RETURNED list={len(r)}"
        return f"DATA_RETURNED {type(r).__name__}"
    except Exception as exc:  # intentional attack surface probe
        return f"EXCEPTION {type(exc).__name__}"


@pytest.mark.skipif(not H1.is_file(), reason="sealed crypto artifacts absent")
def test_r5_full_attack_matrix_all_exceptions_or_control():
    results: list[tuple[str, str]] = []

    # R2-1: direct 1h into view_from_table
    t1h = pq.ParquetFile(H1).read().slice(0, 3)
    results.append(
        (
            "R2_1h_view_from_table",
            _probe(lambda: CryptoVenueAdapter("upbit_krw").view_from_table(t1h)),
        )
    )

    # R2-2: unlabeled 1d via adapter load_shard
    one = pq.ParquetFile(D1).read().slice(0, 1)
    stripped = one.replace_schema_metadata(None)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        rel = "ohlcv/upbit_krw/2024/u.parquet"
        path = root / rel
        path.parent.mkdir(parents=True)
        pq.write_table(stripped, path)
        entry = ManifestEntry(
            relative_path=rel,
            file_sha256=sha256_bytes(path.read_bytes()),
            row_count=1,
            dataset="ohlcv",
            market="upbit_krw",
            year=2024,
        )
        results.append(
            (
                "R2_unlabeled_load_shard",
                _probe(lambda: CryptoVenueAdapter("upbit_krw").load_shard(root, entry)),
            )
        )

        # R3: bare ContractBackedCorpusAdapter + unlabeled 1h
        hpath = root / "ohlcv/upbit_krw/2024/h.parquet"
        htab = pq.ParquetFile(H1).read().slice(0, 1).replace_schema_metadata(None)
        pq.write_table(htab, hpath)
        hentry = ManifestEntry(
            relative_path="ohlcv/upbit_krw/2024/h.parquet",
            file_sha256=sha256_bytes(hpath.read_bytes()),
            row_count=1,
            dataset="ohlcv",
            market="upbit_krw",
            year=2024,
        )
        bare = ContractBackedCorpusAdapter(
            corpus=CorpusKind.CRYPTO_V1,
            holdout_policy=CRYPTO_HOLDOUT_POLICY,
        )
        results.append(
            (
                "R3_ContractBackedCorpusAdapter",
                _probe(lambda: bare.load_shard(root, hentry)),
            )
        )
        results.append(
            (
                "R3_loader_load_shard",
                _probe(
                    lambda: load_shard(
                        root,
                        hentry,
                        corpus=CorpusKind.CRYPTO_V1,
                        holdout_policy=CRYPTO_HOLDOUT_POLICY,
                    )
                ),
            )
        )

        # R4: policy-stripped temp contract cannot be supplied
        results.append(
            (
                "R4_contract_path_kwarg",
                _probe(
                    lambda: load_shard(
                        root,
                        hentry,
                        contract_path="/tmp/stripped.json",  # type: ignore[call-arg]
                        holdout_policy=CRYPTO_HOLDOUT_POLICY,
                    )
                ),
            )
        )

        # R5: corpus_id swap via temp contract cannot be supplied
        results.append(
            (
                "R5_ContractBackedCorpusAdapter_path_ctor",
                _probe(
                    lambda: ContractBackedCorpusAdapter(
                        contract_path="/tmp/kr-spoof.json",  # type: ignore[call-arg]
                        holdout_policy=CRYPTO_HOLDOUT_POLICY,
                    )
                ),
            )
        )

        # control: labeled 1d
        ok = root / "ohlcv/upbit_krw/2024/ok.parquet"
        pq.write_table(one, ok)
        ok_entry = ManifestEntry(
            relative_path="ohlcv/upbit_krw/2024/ok.parquet",
            file_sha256=sha256_bytes(ok.read_bytes()),
            row_count=1,
            dataset="ohlcv",
            market="upbit_krw",
            year=2024,
        )
        results.append(
            (
                "CONTROL_labeled_1d",
                _probe(
                    lambda: CryptoVenueAdapter("upbit_krw").load_shard(root, ok_entry)
                ),
            )
        )

    for name, outcome in results:
        print(f"{name}: {outcome}")

    attacks = [n for n, o in results if n != "CONTROL_labeled_1d"]
    for name in attacks:
        outcome = dict(results)[name]
        assert outcome.startswith("EXCEPTION"), f"{name} still open: {outcome}"
    assert dict(results)["CONTROL_labeled_1d"].startswith("DATA_RETURNED")


def test_no_public_contract_path_parameters_remain():
    import loader
    import market_adapters.common as common
    import schema_contract as sc

    for fn in (
        sc.load_contract,
        sc.validate_table_schema,
        sc.arrow_schema_for,
        sc.date_column_for,
        sc.enforce_table_load_policy,
        loader.load_shard,
    ):
        params = inspect.signature(fn).parameters
        assert "contract_path" not in params, fn.__name__
        if fn is not sc.load_contract:
            # most take corpus=
            assert "corpus" in params or fn is loader.load_shard
    assert "corpus" in inspect.signature(loader.load_shard).parameters
    fields = common.ContractBackedCorpusAdapter.__dataclass_fields__
    assert "corpus" in fields
    assert "contract_path" not in fields
