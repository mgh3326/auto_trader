from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from research.toss_phase2.collect import (
    STAGING_CONTRACT,
    VALUE_SEMANTICS,
    write_page,
)
from research.toss_phase2.load import (
    SOURCE,
    TARGET_TABLE,
    DatabaseLoadError,
    LoadStopped,
    StagingValidationError,
    _assert_database_ready,
    freeze_completed_fragments,
    preflight_source,
)


def _row(*, timestamp: datetime, segment: str = "KRX_REGULAR") -> dict[str, object]:
    return {
        "time_utc": timestamp,
        "session_date_kst": date(2026, 8, 3),
        "symbol": "005930",
        "session_segment": segment,
        "source": SOURCE,
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume": 5.0,
        "value": 55.0,
        "value_semantics": VALUE_SEMANTICS,
        "is_padding": False,
        "pre_nxt": None,
        "retrieved_at": datetime(2026, 8, 4, tzinfo=UTC),
        "batch_id": "toss-staging-batch",
    }


def _staging(tmp_path: Path) -> tuple[Path, Path]:
    staging_dir = tmp_path / "staging"
    state_dir = tmp_path / "loader-state"
    staging_dir.mkdir()
    (staging_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_state": STAGING_CONTRACT,
                "batch_id": "toss-staging-batch",
            }
        )
    )
    return staging_dir, state_dir


def _write_page(
    staging_dir: Path,
    *,
    request_before: str,
    timestamp: datetime,
    segment: str = "KRX_REGULAR",
    row_overrides: dict[str, object] | None = None,
) -> Path:
    row = _row(timestamp=timestamp, segment=segment)
    row.update(row_overrides or {})
    path, reused = write_page(
        staging_dir=staging_dir,
        symbol="005930",
        request_before=request_before,
        next_before="next",
        rows=[row],
        batch_id="toss-staging-batch",
    )
    assert path is not None
    assert reused is False
    return path


def _freeze(
    staging_dir: Path,
    state_dir: Path,
    page: Path,
    *,
    seconds_after: int = 2,
) -> dict[str, object]:
    stat = page.stat()
    return freeze_completed_fragments(
        staging_dir=staging_dir,
        state_dir=state_dir,
        min_fragment_age_seconds=1,
        now_ns=stat.st_mtime_ns + seconds_after * 1_000_000_000,
    )


def test_freeze_selects_only_completed_old_enough_pages_without_staging_writes(
    tmp_path: Path,
) -> None:
    staging_dir, state_dir = _staging(tmp_path)
    old = _write_page(
        staging_dir,
        request_before="old",
        timestamp=datetime(2026, 8, 3, 6, 0, tzinfo=UTC),
    )
    recent = _write_page(
        staging_dir,
        request_before="recent",
        timestamp=datetime(2026, 8, 3, 6, 1, tzinfo=UTC),
    )
    old_stat = old.stat()
    os.utime(old, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns - 5_000_000_000))
    before = {
        path.relative_to(staging_dir): path.read_bytes()
        for path in staging_dir.rglob("*")
        if path.is_file()
    }

    snapshot = _freeze(staging_dir, state_dir, recent, seconds_after=0)

    assert snapshot["completed_fragments_only"] is True
    assert snapshot["source_rows"] == 1
    assert snapshot["too_recent_files_excluded"] == 1
    after = {
        path.relative_to(staging_dir): path.read_bytes()
        for path in staging_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert (state_dir / "snapshot.json").is_file()


def test_preflight_rejects_unclassifiable_session_segment_before_any_db_work(
    tmp_path: Path,
) -> None:
    staging_dir, state_dir = _staging(tmp_path)
    page = _write_page(
        staging_dir,
        request_before="unknown-segment",
        timestamp=datetime(2026, 8, 3, 11, 1, tzinfo=UTC),
        segment="UNKNOWN",
    )
    snapshot = _freeze(staging_dir, state_dir, page)

    with pytest.raises(StagingValidationError, match="session_segment_unclassifiable"):
        preflight_source(
            staging_dir=staging_dir,
            state_dir=state_dir,
            snapshot=snapshot,
        )


def test_preflight_rejects_duplicate_source_key(
    tmp_path: Path,
) -> None:
    staging_dir, state_dir = _staging(tmp_path)
    first = _write_page(
        staging_dir,
        request_before="first",
        timestamp=datetime(2026, 8, 3, 6, 2, tzinfo=UTC),
    )
    second = _write_page(
        staging_dir,
        request_before="second",
        timestamp=datetime(2026, 8, 3, 6, 2, tzinfo=UTC),
    )
    second_stat = second.stat()
    os.utime(
        second, ns=(second_stat.st_atime_ns, second_stat.st_mtime_ns - 5_000_000_000)
    )
    snapshot = _freeze(staging_dir, state_dir, first, seconds_after=5)

    with pytest.raises(StagingValidationError, match="duplicate_staging_key"):
        preflight_source(
            staging_dir=staging_dir,
            state_dir=state_dir,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("row_overrides", "reason"),
    [
        ({"volume": -1.0, "value": -11.0}, "negative_volume"),
        ({"high": 10.0}, "incoherent_ohlc_values"),
        ({"value": 54.0}, "synthetic_value_mismatch"),
    ],
)
def test_preflight_rejects_invalid_numeric_candle_invariants(
    tmp_path: Path,
    row_overrides: dict[str, object],
    reason: str,
) -> None:
    staging_dir, state_dir = _staging(tmp_path)
    page = _write_page(
        staging_dir,
        request_before="invalid-numeric",
        timestamp=datetime(2026, 8, 3, 6, 4, tzinfo=UTC),
        row_overrides=row_overrides,
    )
    snapshot = _freeze(staging_dir, state_dir, page)

    with pytest.raises(StagingValidationError, match=reason):
        preflight_source(
            staging_dir=staging_dir,
            state_dir=state_dir,
            snapshot=snapshot,
        )


def test_preflight_reuses_completed_checkpoint_without_reloading_source(
    tmp_path: Path,
) -> None:
    staging_dir, state_dir = _staging(tmp_path)
    page = _write_page(
        staging_dir,
        request_before="only",
        timestamp=datetime(2026, 8, 3, 6, 3, tzinfo=UTC),
    )
    snapshot = _freeze(staging_dir, state_dir, page)

    preflight_source(
        staging_dir=staging_dir,
        state_dir=state_dir,
        snapshot=snapshot,
    )
    preflight_source(
        staging_dir=staging_dir,
        state_dir=state_dir,
        snapshot=snapshot,
    )


def test_loader_has_no_main_or_public_write_surface() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "research" / "toss_phase2" / "load.py"
    ).read_text()

    assert 'TARGET_TABLE = "research.kr_candles_1m_toss"' in source
    main_write = re.compile(
        r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+research\.kr_candles_1m(?=\s|$)",
        re.IGNORECASE,
    )
    public_write = re.compile(
        r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+public\.", re.IGNORECASE
    )
    # A mutation must match these guards; otherwise the source assertion below
    # could pass due to an inert pattern rather than an absent write surface.
    assert main_write.search("INSERT INTO research.kr_candles_1m (time_utc)")
    assert public_write.search("DELETE FROM public.orders")
    assert not main_write.search(source)
    assert not public_write.search(source)
    assert "session_segment_unclassifiable" in source
    assert "count(DISTINCT (time_utc, symbol))" in source


@pytest.mark.asyncio
async def test_loader_refuses_a_nonempty_target_before_initial_load() -> None:
    class Connection:
        async def fetchval(self, query: str, *args: object) -> object:
            if query == "SELECT current_user":
                return "auto_trader_kr_backfill"
            if query == "SELECT to_regclass($1)":
                assert args == (TARGET_TABLE,)
                return TARGET_TABLE
            if query.startswith("SELECT has_table_privilege"):
                return True
            if query == f"SELECT count(*) FROM {TARGET_TABLE}":
                return 1
            raise AssertionError(f"unexpected query: {query}")

    checkpoint = {"initial_target_rows": None}
    with pytest.raises(DatabaseLoadError, match="nonempty_target_at_initial_load"):
        await _assert_database_ready(
            Connection(),
            expected_source_rows=1,
            checkpoint=checkpoint,
        )
    assert checkpoint["initial_target_rows"] is None


def test_invalid_minimum_fragment_age_stops_with_structured_reason(
    tmp_path: Path,
) -> None:
    with pytest.raises(LoadStopped, match="min_fragment_age_seconds_must_be_positive"):
        freeze_completed_fragments(
            staging_dir=tmp_path / "staging",
            state_dir=tmp_path / "state",
            min_fragment_age_seconds=0,
        )
