"""T3-grade holdout refusal tests — 9 bypass categories + date/type gates.

Path bypass categories (all must raise HoldoutPathBlocked):
1. root
2. child
3. abs
4. rel
5. ``..``
6. ``.`` segment
7. symlink-to-dir
8. symlink-to-file
9. CASE(UPPER / Title / Mixed)

Plus: date/range, datetime→HoldoutDateBlocked, load_manifest dual gate,
load_shard year pre-parse, exception-not-filter.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

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
    assert_partition_year_not_holdout,
    assert_path_not_holdout,
    assert_range_not_holdout,
    holdout_root_resolved,
)
from loader import (
    LOADER_ENTRYPOINTS,
    ManifestEntry,
    load_manifest,
    load_shard,
)
from schema_contract import arrow_schema_for

# --------------------------------------------------------------------------- #
# 9 path-bypass categories (verifier matrix)
# --------------------------------------------------------------------------- #


def test_bypass_01_root_blocked():
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(HOLDOUT_DIR)


def test_bypass_02_child_blocked():
    child = HOLDOUT_DIR / "ohlcv" / "KOSPI" / "2025" / "bars.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(child)


def test_bypass_03_abs_string_blocked():
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(str(HOLDOUT_DIR / "anything.parquet"))


def test_bypass_04_rel_under_holdout_string_blocked(tmp_path, monkeypatch):
    """Relative path that resolves into holdout after cwd join / Path form."""
    monkeypatch.chdir(tmp_path)
    # Relative climb into a name that casefolds to holdout under a parent
    # we control via symlink → holdout root (synthetic; no real holdout I/O).
    parent_link = tmp_path / "artifacts"
    try:
        parent_link.symlink_to(holdout_root_resolved().parent, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlink in this environment")
    rel = Path("artifacts") / "holdout" / "shard.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(rel)
    # Path object of absolute holdout also blocked.
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(Path(str(HOLDOUT_DIR)))


def test_bypass_05_dotdot_blocked():
    sneaky = HOLDOUT_DIR / ".." / "holdout" / "secret.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(sneaky)


def test_bypass_06_dot_segment_blocked():
    sneaky = HOLDOUT_DIR / "." / "nested" / "." / "x.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(sneaky)
    # middle-dot form via string
    s = str(HOLDOUT_DIR).rstrip("/") + "/./y.parquet"
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(s)


def test_bypass_07_symlink_to_dir_blocked(tmp_path):
    link = tmp_path / "escape_link"
    target = holdout_root_resolved()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlink in this environment")
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(link)


def test_bypass_08_symlink_to_file_blocked(tmp_path):
    link = tmp_path / "holdout_file.parquet"
    target = holdout_root_resolved() / "fake.parquet"
    try:
        link.symlink_to(target, target_is_directory=False)
    except OSError:
        pytest.skip("cannot create symlink in this environment")
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(link)


def _holdout_case_variants() -> list[tuple[str, str]]:
    """UPPER / Title / Mixed variants of the terminal 'holdout' segment."""
    base = str(HOLDOUT_DIR).rstrip("/")
    assert base.endswith("holdout"), "fixture path literal changed"
    parent = base[: -len("holdout")]
    return [
        ("UPPER", parent + "HOLDOUT"),
        ("Title", parent + "Holdout"),
        ("Mixed", parent + "HoLdOuT"),
    ]


@pytest.mark.parametrize("label,variant", _holdout_case_variants())
def test_bypass_09_case_variant_root_blocked(label: str, variant: str):
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(variant)


@pytest.mark.parametrize("label,variant", _holdout_case_variants())
def test_bypass_09_case_variant_child_blocked(label: str, variant: str):
    with pytest.raises(HoldoutPathBlocked):
        assert_path_not_holdout(variant + "/ohlcv/2025/bars.parquet")


def test_loader_refuses_absolute_holdout_relative_path(tmp_path):
    """load_shard refuses absolute holdout path (year kept non-holdout so path fires)."""
    entry = ManifestEntry(
        relative_path=str(HOLDOUT_DIR / "bars.parquet"),
        file_sha256="a" * 64,
        row_count=0,
        dataset="ohlcv",
        market="KOSPI",
        year=2024,  # path gate must fire even when year is exploration
    )
    with pytest.raises(HoldoutPathBlocked):
        load_shard(tmp_path, entry)


def test_loader_refuses_case_variant_absolute_path(tmp_path):
    upper = str(HOLDOUT_DIR).rstrip("/").replace("holdout", "HOLDOUT") + "/x.parquet"
    entry = ManifestEntry(
        relative_path=upper,
        file_sha256="a" * 64,
        row_count=0,
        dataset="ohlcv",
        market="KOSPI",
        year=2024,
    )
    with pytest.raises(HoldoutPathBlocked):
        load_shard(tmp_path, entry)


# --------------------------------------------------------------------------- #
# Date / range / datetime type
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
    with pytest.raises(HoldoutDateBlocked):
        assert_range_not_holdout("2024-12-01", "2025-01-15")


def test_range_covering_entire_holdout_is_blocked():
    with pytest.raises(HoldoutDateBlocked):
        assert_range_not_holdout("2015-01-01", "2026-07-31")


def test_datetime_holdout_raises_holdout_date_blocked_not_typeerror():
    """SHOULD-3: datetime in holdout window → HoldoutDateBlocked, not TypeError."""
    with pytest.raises(HoldoutDateBlocked):
        assert_date_not_holdout(datetime(2025, 6, 15, 12, 30, 0))
    # Must not be TypeError
    try:
        assert_date_not_holdout(datetime(2025, 1, 1, 0, 0, 0))
    except TypeError:
        pytest.fail("datetime holdout raised TypeError; must be HoldoutDateBlocked")
    except HoldoutDateBlocked:
        pass


def test_datetime_exploration_accepted():
    assert assert_date_not_holdout(datetime(2024, 6, 15, 9, 0, 0)) == date(2024, 6, 15)


def test_unsupported_date_type_is_holdout_date_blocked_not_typeerror():
    with pytest.raises(HoldoutDateBlocked):
        assert_date_not_holdout(12345)  # type: ignore[arg-type]


def test_partition_year_holdout_blocked():
    with pytest.raises(HoldoutDateBlocked):
        assert_partition_year_not_holdout(2025)
    with pytest.raises(HoldoutDateBlocked):
        assert_partition_year_not_holdout(2026)
    assert assert_partition_year_not_holdout(2024) == 2024
    assert assert_partition_year_not_holdout(2023) == 2023


# --------------------------------------------------------------------------- #
# Window boundary
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


# --------------------------------------------------------------------------- #
# Loader entrypoints dual gate
# --------------------------------------------------------------------------- #


def test_loader_entrypoints_enumerated_complete():
    """Public loader surface is exactly the dual-gated set."""
    assert LOADER_ENTRYPOINTS == ("load_manifest", "load_shard")


def test_load_manifest_refuses_holdout_path(tmp_path):
    # Do not create files under real HOLDOUT_DIR — only pass the path string.
    with pytest.raises(HoldoutPathBlocked):
        load_manifest(HOLDOUT_DIR / "manifest.json")


def test_load_manifest_refuses_holdout_partition_year(tmp_path):
    """Date gate on load_manifest: year intersecting holdout refused."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "relative_path": "ohlcv/KOSPI/2025/bars.parquet",
                    "file_sha256": "a" * 64,
                    "row_count": 0,
                    "dataset": "ohlcv",
                    "market": "KOSPI",
                    "year": 2025,
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(HoldoutDateBlocked):
        load_manifest(manifest)


def test_load_manifest_refuses_entry_absolute_holdout_path(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "relative_path": str(HOLDOUT_DIR / "bars.parquet"),
                    "file_sha256": "a" * 64,
                    "row_count": 0,
                    "dataset": "ohlcv",
                    "market": "KOSPI",
                    "year": 2024,
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(HoldoutPathBlocked):
        load_manifest(manifest)


def test_load_shard_refuses_holdout_year_before_open(tmp_path):
    """Year gate fires even if a non-holdout path file exists."""
    rel = "ohlcv/KOSPI/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not-even-parquet")
    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=hashlib.sha256(b"not-even-parquet").hexdigest(),
        row_count=0,
        dataset="ohlcv",
        market="KOSPI",
        year=2025,  # lie year → date gate before open
    )
    with pytest.raises(HoldoutDateBlocked):
        load_shard(tmp_path, entry)


def test_loader_refuses_table_containing_holdout_date(tmp_path):
    """Row-level date gate: year exploration but session_date in holdout."""
    schema = arrow_schema_for("ohlcv")
    rows = [
        {
            "session": "2025-01-01",  # holdout
            "market": "KOSPI",
            "ticker": "005930",
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": 1,
            "value": 1,
            "price_mode": "adjusted",
            "source_product": "synthetic_fixture",
        }
    ]
    table = pa.Table.from_pylist(rows, schema=schema)
    rel = "ohlcv/KOSPI/2024/bars.parquet"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    pq.write_table(table, path)
    data = path.read_bytes()

    entry = ManifestEntry(
        relative_path=rel,
        file_sha256=hashlib.sha256(data).hexdigest(),
        row_count=1,
        dataset="ohlcv",
        market="KOSPI",
        year=2024,
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
