"""Hard, non-configurable backfill contamination and cursor-overlap guards."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECT_PATH = REPOSITORY_ROOT / "research" / "kr_backfill" / "collect.py"


@pytest.fixture
def collect_module() -> ModuleType:
    module_name = "kr_backfill_collect_hard_ratio_guard_test"
    sys.path.insert(0, str(COLLECT_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, COLLECT_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(COLLECT_PATH.parent))


def test_db_insert_conflict_ratio_aborts_above_half_percent(
    collect_module: ModuleType,
) -> None:
    assert collect_module.DB_INSERT_CONFLICT_ABORT_RATIO == 0.005
    at_limit = collect_module.SurfaceStats(
        surface="mock", rows_inserted=995, rows_skipped_conflict=5
    )
    collect_module.enforce_hard_ratio_guards(at_limit)

    above_limit = collect_module.SurfaceStats(
        surface="mock", rows_inserted=994, rows_skipped_conflict=6
    )
    with pytest.raises(collect_module.AbortStream, match="DB insert conflict ratio"):
        collect_module.enforce_hard_ratio_guards(above_limit)


def test_cursor_overlap_ratio_aborts_above_forty_percent(
    collect_module: ModuleType,
) -> None:
    assert collect_module.CURSOR_OVERLAP_ABORT_RATIO == 0.40
    at_limit = collect_module.StreamStats(
        source="kiwoom", rows_kept=600, rows_filtered_cursor_overlap=400
    )
    collect_module.enforce_hard_ratio_guards(at_limit)

    above_limit = collect_module.StreamStats(
        source="kiwoom", rows_kept=599, rows_filtered_cursor_overlap=401
    )
    with pytest.raises(collect_module.AbortStream, match="cursor overlap ratio"):
        collect_module.enforce_hard_ratio_guards(above_limit)


def test_only_proven_first_page_resume_preseed_is_excluded(
    collect_module: ModuleType,
) -> None:
    preseed = collect_module.classify_resume_preseed_conflicts(
        first_page=True,
        checkpoint_was_partial=True,
        rows_kept=702,
        rows_inserted=0,
        rows_skipped_conflict=702,
        rows_verified_between_checkpoint_and_restart=702,
    )
    assert preseed == 702
    collect_module.enforce_hard_ratio_guards(
        collect_module.SurfaceStats(
            surface="live",
            rows_skipped_conflict=702,
            rows_skipped_conflict_preseed=preseed,
        )
    )

    assert (
        collect_module.classify_resume_preseed_conflicts(
            first_page=False,
            checkpoint_was_partial=True,
            rows_kept=702,
            rows_inserted=0,
            rows_skipped_conflict=702,
            rows_verified_between_checkpoint_and_restart=702,
        )
        == 0
    )
    assert (
        collect_module.classify_resume_preseed_conflicts(
            first_page=True,
            checkpoint_was_partial=True,
            rows_kept=702,
            rows_inserted=0,
            rows_skipped_conflict=702,
            rows_verified_between_checkpoint_and_restart=701,
        )
        == 0
    )
    with pytest.raises(collect_module.AbortStream, match="DB insert conflict ratio"):
        collect_module.enforce_hard_ratio_guards(
            collect_module.SurfaceStats(
                surface="live",
                rows_inserted=994,
                rows_skipped_conflict=708,
                rows_skipped_conflict_preseed=702,
            )
        )


def test_both_collection_paths_enforce_non_configurable_hard_guards(
    collect_module: ModuleType,
) -> None:
    assert list(
        inspect.signature(collect_module.enforce_hard_ratio_guards).parameters
    ) == ["stats"]
    assert "enforce_hard_ratio_guards(stats)" in inspect.getsource(
        collect_module.run_stream
    )
    assert "enforce_hard_ratio_guards(stats)" in inspect.getsource(
        collect_module.run_surface
    )
