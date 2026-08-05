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
    StagingValidationError,
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
) -> Path:
    path, reused = write_page(
        staging_dir=staging_dir,
        symbol="005930",
        request_before=request_before,
        next_before="next",
        rows=[_row(timestamp=timestamp, segment=segment)],
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
    before = old.read_bytes()

    snapshot = _freeze(staging_dir, state_dir, recent, seconds_after=0)

    assert snapshot["completed_fragments_only"] is True
    assert snapshot["source_rows"] == 1
    assert snapshot["too_recent_files_excluded"] == 1
    assert old.read_bytes() == before
    assert not (staging_dir / "loader-state").exists()


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
    assert not re.search(
        r"(?:INSERT INTO|UPDATE|DELETE FROM)\\s+research\\.kr_candles_1m(?:\\s|$)",
        source,
    )
    assert not re.search(r"(?:INSERT INTO|UPDATE|DELETE FROM)\\s+public\\.", source)
    assert "session_segment_unclassifiable" in source
