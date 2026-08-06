from __future__ import annotations

import pytest

from research.kr_corpus.d3_engine.acceptance import run_acceptance
from research.kr_corpus.d3_engine.constants import ArtifactPaths

_PATHS = ArtifactPaths.defaults()
_REQUIRED = (
    _PATHS.contract_v3,
    _PATHS.contract_v2,
    _PATHS.baseline,
    _PATHS.index_csv,
    _PATHS.tick_yaml,
    _PATHS.tick_python_provenance,
    _PATHS.golden_root / "CONTRACT.md",
    _PATHS.golden_root / "provenance.json",
    _PATHS.golden_root / "checksums.sha256",
)

pytestmark = pytest.mark.skipif(
    not all(path.is_file() for path in _REQUIRED),
    reason="external immutable D3 artifact is not available on this runner",
)


def test_d3_e1_full_local_acceptance() -> None:
    result = run_acceptance()

    assert result["sha_gate"]["passed"] == 9
    assert result["golden_files"] == {
        "vectors": 33,
        "expected": 33,
        "files": 69,
        "checksums_passed": 68,
        "checksums_total": 68,
        "ids_match": True,
    }
    assert result["golden_exact"]["passed"] == 33
    assert result["deterministic_2runs"] is True
    assert result["four_arms"] == {"B0": "OK", "C1": "OK", "C2": "OK", "C3": "OK"}
    assert result["mutant_diffs"]["passed"] == 23
    assert all(row["status"] == "PASS" for row in result["negative_tests"].values())
    assert result["sealed_access_spy"] == 0
    assert result["engine_input_explanation_keys"] == 0
    assert result["fib_window_excludes_t"] is True
    assert result["tick_source"]["source"] == "krx_tick_table_frozen.yaml"
    assert result["tick_source"]["python_import_count"] == 0
    assert result["primary_run_executed"] is False
