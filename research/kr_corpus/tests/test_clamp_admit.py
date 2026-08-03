from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.kr_corpus.clamp_admit import (
    ClampAdmitBuildError,
    build_clamp_admit_view,
    verify_clamp_admit_view,
)


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _main_source(tmp_path: Path) -> Path:
    source = tmp_path / "main"
    (source / "manifest.json").parent.mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps({"scope": "main", "corpus_id": "kr-corpus-v1"}),
        encoding="utf-8",
    )
    _write_parquet(
        source / "dataset/market=KOSPI/year=2024/ticker=111111.parquet",
        [
            {
                "session": "2024-01-02",
                "market": "KOSPI",
                "ticker": "111111",
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 100,
                "value": None,
                "price_mode": "adjusted",
                "source_product": "pykrx",
            }
        ],
    )
    _write_parquet(
        source / "gaps/market=KOSPI/year=2024/missing.parquet",
        [
            {
                "session": "2024-01-03",
                "market": "KOSPI",
                "ticker": "111111",
                "reason": "ohlc_invariant_violation",
                "detail": "source invariant violation",
            }
        ],
    )
    _write_parquet(
        source / "gaps/market=KOSDAQ/year=2024/missing.parquet",
        [
            {
                "session": "2024-01-03",
                "market": "KOSDAQ",
                "ticker": "222222",
                "reason": "ohlc_invariant_violation",
                "detail": "source invariant violation",
            }
        ],
    )
    anomalies = [
        {
            "kind": "ohlc_invariant_violation",
            "session": "2024-01-03",
            "market": "UNMAPPED",
            "ticker": "111111",
            "detail": {
                "session": "2024-01-03",
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 110,
                "volume": 10,
                "value": None,
            },
        },
        {
            "kind": "ohlc_invariant_violation",
            "session": "2024-01-03",
            "market": "UNMAPPED",
            "ticker": "222222",
            "detail": {
                "session": "2024-01-03",
                "open": 0,
                "high": 0,
                "low": 0,
                "close": 10,
                "volume": 0,
                "value": None,
            },
        },
    ]
    (source / "source-anomalies.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in anomalies),
        encoding="utf-8",
    )
    return source


def test_clamp_admit_keeps_source_view_unchanged_and_excludes_no_trade(
    tmp_path: Path,
):
    source = _main_source(tmp_path)
    derived = tmp_path / "derived"

    result = build_clamp_admit_view(source, derived)

    assert result.source_valid_bar_rows == 1
    assert result.clamp_rows_admitted == 1
    assert result.no_trade_rows_excluded == 1
    assert result.clamp_admit_rows == 2
    assert (derived / "checksums.sha256").is_file()
    assert (derived / "manifest.json").is_file()

    source_rows = (
        pq.ParquetFile(source / "dataset/market=KOSPI/year=2024/ticker=111111.parquet")
        .read()
        .to_pylist()
    )
    assert source_rows == [
        {
            "session": "2024-01-02",
            "market": "KOSPI",
            "ticker": "111111",
            "open": 10,
            "high": 12,
            "low": 9,
            "close": 11,
            "volume": 100,
            "value": None,
            "price_mode": "adjusted",
            "source_product": "pykrx",
        }
    ]

    derived_rows = (
        pq.ParquetFile(derived / "dataset/market=KOSPI/year=2024/ticker=111111.parquet")
        .read()
        .to_pylist()
    )
    clamped = next(row for row in derived_rows if row["clamped"])
    assert clamped["high"] == 110
    assert clamped["low"] == 95
    assert clamped["source_high"] == 105
    assert clamped["source_low"] == 95
    assert clamped["clamp_delta_high"] == 5
    assert clamped["clamp_delta_low"] == 0
    assert clamped["admitted"] is True
    assert clamped["clamp_classification"] == "tradeable_adjusted_rounding"

    no_trade_rows = (
        pq.ParquetFile(derived / "no_trade/market=KOSDAQ/year=2024/excluded.parquet")
        .read()
        .to_pylist()
    )
    assert no_trade_rows[0]["clamp_classification"] == "no_trade"
    assert no_trade_rows[0]["clamped"] is False
    assert no_trade_rows[0]["admitted"] is False

    manifest = json.loads((derived / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_valid_bar_view_unchanged"] is True
    assert manifest["clamp_admit_rows"] == 2
    assert manifest["row_contract"]["clamped_column"] == "data_column"
    assert verify_clamp_admit_view(derived) == {
        "checksums_verified": True,
        "source_valid_bar_rows": 1,
        "clamp_rows_admitted": 1,
        "no_trade_rows_excluded": 1,
        "clamp_admit_rows": 2,
    }


def test_clamp_admit_refuses_holdout_source_root(tmp_path: Path):
    source = _main_source(tmp_path / "holdout")

    with pytest.raises(ClampAdmitBuildError, match="holdout"):
        build_clamp_admit_view(source, tmp_path / "derived")
    with pytest.raises(ClampAdmitBuildError, match="holdout"):
        verify_clamp_admit_view(tmp_path / "holdout" / "derived")
