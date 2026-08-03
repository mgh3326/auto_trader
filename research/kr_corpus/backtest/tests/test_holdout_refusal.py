"""T3-grade holdout refusal tests.

Four bypass classes — all must raise exceptions (not silent filter):
1. Path under HOLDOUT_DIR
2. Date / date-range inside HOLDOUT_WINDOW
3. Symlink / relative / ``..`` normalization path bypass
4. Window boundary: 2024-12-31 allowed, 2025-01-01 refused
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from holdout_guard import (
    HOLDOUT_DIR,
    HOLDOUT_END,
    HOLDOUT_START,
    HoldoutDateBlocked,
    HoldoutPathBlocked,
    assert_date_not_holdout,
    assert_path_not_holdout,
    assert_range_not_holdout,
    holdout_root_resolved,
)
from loader import ManifestEntry, load_shard
from schema_contract import arrow_schema_for

# --------------------------------------------------------------------------- #
# 1. Path under HOLDOUT_DIR
# --------------------------------------------------------------------------- #


def test_holdout_dir_root_path_is_blocked():
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(HOLDOUT_DIR)


def test_holdout_dir_child_path_is_blocked():
    child = HOLDOUT_DIR / "ohlcv" / "KOSPI" / "2025" / "bars.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(child)


def test_holdout_dir_as_string_is_blocked():
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(str(HOLDOUT_DIR / "anything.parquet"))


# --------------------------------------------------------------------------- #
# 2. Date / range inside HOLDOUT_WINDOW
# --------------------------------------------------------------------------- #


def test_holdout_mid_date_is_blocked():
    with pytest.raises(HoldoutDateBlocked):
        assert_date_not_holdout(date(2025, 6, 15))


def test_holdout_end_date_is_blocked():
    with pytest.raises(HoldoutDateBlocked):
        assert_date_not_holdout(HOLDOUT_END)


def test_range_fully_inside_holdout_is_blocked():
    with pytest.raises(HoldoutDateBlocked):
        assert_range_not_holdout("2025-01-01", "2025-12-31")


def test_range_partially_overlapping_holdout_is_blocked():
    # Exploration end + holdout start straddling range.
    with pytest.raises(HoldoutDateBlocked):
        assert_range_not_holdout("2024-12-01", "2025-01-15")


def test_range_covering_entire_holdout_is_blocked():
    with pytest.raises(HoldoutDateBlocked):
        assert_range_not_holdout("2015-01-01", "2026-07-31")


# --------------------------------------------------------------------------- #
# 3. Symlink / relative / ``..`` normalization
# --------------------------------------------------------------------------- #


def test_relative_dotdot_normalization_cannot_reach_holdout(tmp_path):
    """A relative path that resolves into HOLDOUT_DIR must be blocked."""
    # Build a path string that, after resolve, lands under holdout.
    # We do not create files inside HOLDOUT_DIR; we only resolve strings.
    sneaky = HOLDOUT_DIR / ".." / "holdout" / "secret.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(sneaky)


def test_symlink_into_holdout_is_blocked(tmp_path):
    """If a symlink resolves under HOLDOUT_DIR, refuse before read."""
    # Create a symlink that points at HOLDOUT_DIR (may not exist — still
    # resolve to that path on macOS when the parent chain exists, or when
    # we point at a real ancestor). We only need the *resolved* target to
    # be under holdout_root.
    link = tmp_path / "escape_link"
    target = holdout_root_resolved()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlink in this environment")
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(link)


def test_symlink_to_holdout_file_is_blocked(tmp_path):
    link = tmp_path / "holdout_file.parquet"
    target = holdout_root_resolved() / "fake.parquet"
    try:
        link.symlink_to(target, target_is_directory=False)
    except OSError:
        pytest.skip("cannot create symlink in this environment")
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(link)


def test_loader_refuses_absolute_holdout_relative_path(tmp_path):
    """load_shard refuses when relative_path is an absolute holdout path."""
    entry = ManifestEntry(
        relative_path=str(HOLDOUT_DIR / "bars.parquet"),
        file_sha256="a" * 64,
        row_count=0,
        dataset="ohlcv",
        market="KOSPI",
        year=2025,
    )
    with pytest.raises(HoldoutPathBlocked):
        load_shard(tmp_path, entry)


# --------------------------------------------------------------------------- #
# 4. Window boundary: 2024-12-31 allowed / 2025-01-01 refused
# --------------------------------------------------------------------------- #


def test_boundary_20241231_allowed():
    assert assert_date_not_holdout(date(2024, 12, 31)) == date(2024, 12, 31)
    assert assert_date_not_holdout("2024-12-31") == date(2024, 12, 31)


def test_boundary_20250101_refused():
    assert HOLDOUT_START == date(2025, 1, 1)
    with pytest.raises(HoldoutDateBlocked):
        assert_date_not_holdout(date(2025, 1, 1))
    with pytest.raises(HoldoutDateBlocked):
        assert_date_not_holdout("2025-01-01")


def test_exploration_range_allowed():
    s, e = assert_range_not_holdout("2015-01-01", "2024-12-31")
    assert s == date(2015, 1, 1)
    assert e == date(2024, 12, 31)


def test_loader_refuses_table_containing_holdout_date(tmp_path):
    """Even if path is outside holdout, a 2025-01-01 row is date-blocked."""
    schema = arrow_schema_for("ohlcv")
    rows = [
        {
            "symbol": "005930",
            "session_date": "2025-01-01",  # holdout
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "trading_value": 1.0,
            "market": "KOSPI",
        }
    ]
    table = pa.Table.from_pylist(rows, schema=schema)
    rel = "ohlcv/KOSPI/2025/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    pq.write_table(table, path)
    data = path.read_bytes()
    import hashlib

    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=hashlib.sha256(data).hexdigest(),
        row_count=1,
        dataset="ohlcv",
        market="KOSPI",
        year=2025,
    )
    with pytest.raises(HoldoutDateBlocked):
        load_shard(tmp_path, entry)


def test_refusal_is_exception_not_silent_empty(tmp_path):
    """Critical: holdout access must not return empty success."""
    with pytest.raises(HoldoutPathBlocked) as exc_info:
        assert_path_not_holdout(HOLDOUT_DIR / "x.parquet")
    assert "holdout" in str(exc_info.value).lower()

    with pytest.raises(HoldoutDateBlocked) as exc_info2:
        assert_date_not_holdout("2025-03-01")
    assert "holdout" in str(exc_info2.value).lower()
