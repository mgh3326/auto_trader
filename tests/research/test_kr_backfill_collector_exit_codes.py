from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECT_PATH = REPOSITORY_ROOT / "research" / "kr_backfill" / "collect.py"


@pytest.fixture
def collect_module() -> ModuleType:
    module_name = "kr_backfill_collect_exit_code_test"
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


def _stats(module: ModuleType, **updates: object) -> object:
    stats = module.StreamStats(source="kiwoom")
    for name, value in updates.items():
        setattr(stats, name, value)
    return stats


def test_all_clean_useful_streams_exit_zero(collect_module: ModuleType) -> None:
    result = _stats(collect_module, rows_fetched=900, rows_inserted=700)

    assert collect_module.exit_code_for_results([result]) == 0


@pytest.mark.parametrize(
    "result",
    [
        None,
        RuntimeError("stream exploded"),
    ],
)
def test_no_useful_rows_is_total_failure(
    collect_module: ModuleType, result: BaseException | None
) -> None:
    value = result if result is not None else _stats(collect_module)

    assert collect_module.exit_code_for_results([value]) == 2


def test_error_before_rows_is_total_failure(collect_module: ModuleType) -> None:
    result = _stats(collect_module, errors=["fetch failed"])

    assert collect_module.exit_code_for_results([result]) == 2


def test_rows_then_error_is_partial_failure(collect_module: ModuleType) -> None:
    result = _stats(
        collect_module,
        rows_fetched=900,
        rows_inserted=700,
        errors=["later fetch failed"],
    )

    assert collect_module.exit_code_for_results([result]) == 1


def test_clean_and_failed_streams_are_partial_failure(
    collect_module: ModuleType,
) -> None:
    clean = _stats(collect_module, rows_fetched=900, rows_inserted=700)
    failed = _stats(collect_module, errors=["fetch failed"])

    assert collect_module.exit_code_for_results([clean, failed]) == 1


def test_stopped_stream_after_rows_is_partial_failure(
    collect_module: ModuleType,
) -> None:
    result = _stats(
        collect_module,
        rows_fetched=900,
        rows_inserted=700,
        stopped_reason="latency abort",
    )

    assert collect_module.exit_code_for_results([result]) == 1


def test_empty_symbol_among_useful_rows_is_partial_failure(
    collect_module: ModuleType,
) -> None:
    result = _stats(
        collect_module,
        rows_fetched=900,
        rows_inserted=700,
        empty_responses=1,
        empty_symbols=["001570"],
    )

    assert collect_module.exit_code_for_results([result]) == 1
