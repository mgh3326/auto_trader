"""Focused tests for the read-only US Parquet adapter and path-access spy."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.us_stage_b.parquet_source import (
    CorpusPathAccessError,
    ParquetUSBarSource,
    PathAccessSpy,
    assert_year_root_allowed,
    is_forbidden_corpus_path,
)


def _write_part(
    directory: Path,
    rows: list[dict],
    *,
    name: str = "part-00000.parquet",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    path = directory / name
    pq.write_table(table, path)
    return path


def _bar_row(
    symbol: str,
    session: date,
    *,
    open_: float = 10.0,
    high: float = 11.0,
    low: float = 9.0,
    close: float = 10.5,
    volume: float = 1_000.0,
) -> dict:
    return {
        "symbol": symbol,
        "session_date": datetime(session.year, session.month, session.day),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_is_forbidden_corpus_path_detects_holdout_and_staging(tmp_path: Path) -> None:
    assert is_forbidden_corpus_path(tmp_path / "holdout" / "year=2025")
    assert is_forbidden_corpus_path(tmp_path / "HOLDOUT" / "x")
    assert is_forbidden_corpus_path(tmp_path / "_staging" / "year=2016")
    assert not is_forbidden_corpus_path(tmp_path / "dataset" / "year=2016")


def test_parquet_source_loads_literal_columns_deterministically(tmp_path: Path) -> None:
    year_root = tmp_path / "dataset" / "market=us" / "year=2016"
    _write_part(
        year_root,
        [
            _bar_row("BBB", date(2016, 1, 5), close=20.0),
            _bar_row("AAA", date(2016, 1, 4), close=10.0),
            _bar_row("AAA", date(2016, 1, 5), close=11.0),
        ],
    )
    # Sibling forbidden trees must never be touched when only year_root is passed.
    (tmp_path / "holdout").mkdir()
    (tmp_path / "_staging").mkdir()
    (tmp_path / "holdout" / "poison.parquet").write_bytes(b"not-parquet")

    spy = PathAccessSpy()
    source = ParquetUSBarSource.from_year_roots([year_root], access_spy=spy)

    assert source.symbols() == ("AAA", "BBB")
    assert source.corpus_sessions() == (date(2016, 1, 4), date(2016, 1, 5))
    bar = source.get("AAA", date(2016, 1, 4))
    assert bar is not None
    assert bar.adjusted_close == 10.0
    assert bar.open == 10.0
    assert bar.volume == 1_000.0
    assert source.get("MISSING", date(2016, 1, 4)) is None

    summary = source.access_summary()
    assert summary["forbidden_root_enumerations"] == 0
    assert summary["holdout_reads"] == 0
    assert summary["rows_loaded"] == 3
    assert spy.forbidden_root_enumerations() == ()
    assert not any(
        "holdout" in record.path.lower() or "_staging" in record.path.lower()
        for record in spy.records
    )


def test_duplicate_rows_fail_closed(tmp_path: Path) -> None:
    year_root = tmp_path / "year=2017"
    _write_part(
        year_root,
        [
            _bar_row("AAA", date(2017, 3, 1)),
            _bar_row("AAA", date(2017, 3, 1)),
        ],
    )
    with pytest.raises(CorpusPathAccessError, match="duplicate"):
        ParquetUSBarSource.from_year_roots([year_root])


def test_null_symbol_and_null_session_fail_closed(tmp_path: Path) -> None:
    year_root = tmp_path / "year=2018"
    _write_part(
        year_root,
        [
            {
                "symbol": None,
                "session_date": datetime(2018, 1, 2),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ],
    )
    with pytest.raises(CorpusPathAccessError, match="invalid symbol"):
        ParquetUSBarSource.from_year_roots([year_root])

    year_root_b = tmp_path / "year=2019"
    _write_part(
        year_root_b,
        [
            {
                "symbol": "AAA",
                "session_date": None,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ],
    )
    with pytest.raises(CorpusPathAccessError, match="null session_date"):
        ParquetUSBarSource.from_year_roots([year_root_b])


def test_missing_required_column_fail_closed(tmp_path: Path) -> None:
    year_root = tmp_path / "year=2020"
    year_root.mkdir()
    table = pa.table(
        {
            "symbol": ["AAA"],
            "session_date": [datetime(2020, 1, 2)],
            "open": [1.0],
            # high missing
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        }
    )
    pq.write_table(table, year_root / "part-00000.parquet")
    with pytest.raises(CorpusPathAccessError, match="lacks required columns"):
        ParquetUSBarSource.from_year_roots([year_root])


def test_year_2025_plus_rows_and_roots_fail_closed(tmp_path: Path) -> None:
    bad_root = tmp_path / "year=2025"
    bad_root.mkdir()
    with pytest.raises(CorpusPathAccessError, match="outside the nine allowlisted"):
        assert_year_root_allowed(bad_root)

    year_root = tmp_path / "year=2024"
    _write_part(
        year_root,
        [_bar_row("AAA", date(2025, 1, 2))],
    )
    with pytest.raises(CorpusPathAccessError, match="year mismatch|2025"):
        ParquetUSBarSource.from_year_roots([year_root])


def test_holdout_and_staging_roots_rejected_without_listdir(tmp_path: Path) -> None:
    holdout_year = tmp_path / "holdout" / "year=2016"
    holdout_year.mkdir(parents=True)
    _write_part(holdout_year, [_bar_row("AAA", date(2016, 1, 4))])
    spy = PathAccessSpy()
    with pytest.raises(CorpusPathAccessError, match="forbidden"):
        ParquetUSBarSource.from_year_roots([holdout_year], access_spy=spy)
    assert all(record.kind != "listdir" for record in spy.records)
    assert all(record.kind != "open_parquet" for record in spy.records)
    assert spy.forbidden_root_enumerations() == ()

    staging_year = tmp_path / "_staging" / "year=2017"
    staging_year.mkdir(parents=True)
    spy2 = PathAccessSpy()
    with pytest.raises(CorpusPathAccessError, match="forbidden"):
        ParquetUSBarSource.from_year_roots([staging_year], access_spy=spy2)
    assert all(record.kind != "listdir" for record in spy2.records)
    assert spy2.forbidden_root_enumerations() == ()


def test_unknown_year_root_name_rejected(tmp_path: Path) -> None:
    root = tmp_path / "not-a-year"
    root.mkdir()
    with pytest.raises(CorpusPathAccessError, match="year=YYYY"):
        assert_year_root_allowed(root)


def test_multi_file_year_root_is_sorted_and_merged(tmp_path: Path) -> None:
    year_root = tmp_path / "year=2021"
    _write_part(
        year_root,
        [_bar_row("ZZZ", date(2021, 2, 1))],
        name="part-00001.parquet",
    )
    _write_part(
        year_root,
        [_bar_row("AAA", date(2021, 1, 4))],
        name="part-00000.parquet",
    )
    source = ParquetUSBarSource.from_year_roots([year_root])
    assert source.symbols() == ("AAA", "ZZZ")
    assert [path.name for path in source.files_read] == [
        "part-00000.parquet",
        "part-00001.parquet",
    ]
