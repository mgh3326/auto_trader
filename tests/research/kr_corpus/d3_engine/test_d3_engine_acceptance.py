from __future__ import annotations

from decimal import Decimal
from pathlib import Path

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
    _PATHS.amendment_a1,
)

pytestmark = pytest.mark.skipif(
    not all(path.is_file() for path in _REQUIRED),
    reason="external immutable D3 artifact is not available on this runner",
)


def test_d3_e1_full_local_acceptance(tmp_path: Path) -> None:
    result = run_acceptance(primary_artifact_root=tmp_path / "not-executed")

    assert result["sha_gate"] == {
        "passed": 10,
        "total": 10,
        "rows": result["sha_gate"]["rows"],
    }
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
    for payload in result["four_arm_contract"].values():
        assert [fill["price"] for fill in payload["fills"]] == [9600, 9450]
        assert payload["signals_submitted"] == 2
        assert payload["terminal_nav"] == Decimal("13520402.57250")
    assert result["engine_contract_probes"] == {
        "sell_fill_prices": [105, 100, None],
        "unitized_mdd": Decimal("-0.2"),
        "resistance_orders": [{"rung": "R1", "limit": 10960, "quantity": 5}],
        "day_expiry": True,
        "receivable_single_credit": True,
        "global_rank_cap": {"demand_pairs": 3, "fills": 6},
        "c2_missing_index_fail_closed": True,
        "c3_buy_suppression_bound": True,
    }
    assert result["mutant_diffs"]["passed"] == 23
    assert result["mutant_diffs"]["total"] == 23
    assert result["correction_golden"]["passed"] == 3
    assert result["correction_golden"]["total"] == 3
    assert result["correction_mutants"]["passed"] == 4
    assert result["correction_mutants"]["total"] == 4
    assert {row["name"] for row in result["correction_mutants"]["cases"]} == {
        "C3-only-clock",
        "missing-close-reset",
        "B0-trim-fires",
        "close-ge-average-keeps-streak",
    }
    assert all(row["status"] == "PASS" for row in result["negative_tests"].values())
    assert result["negative_tests"]["sealed_access"]["paths"] == {
        "holdout": "PASS",
        "D3_CALIBRATION_2025": "PASS",
        "prospective": "PASS",
    }
    assert result["negative_tests"]["sealed_access"]["loader_calls"] == 0
    assert result["negative_tests"]["sealed_access"]["metadata_key_lookups"] == 0
    assert result["sealed_access_spy"] == 0
    assert result["engine_input_explanation_keys"] == 0
    assert result["fib_window_excludes_t"] is True
    assert result["tick_source"]["source"] == "krx_tick_table_frozen.yaml"
    assert result["tick_source"]["python_import_count"] == 0
    assert result["primary_run"]["reason"] == "manifest_missing"
    assert result["primary_run_executed"] is False
