from __future__ import annotations

import ast
import builtins
import csv
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from research.krb1_c_stress_reducer import (
    ContractError,
    load_cost_inputs,
    load_tick_tables,
    run_reducer,
    write_artifacts,
)
from research.krb1_c_stress_reducer import reducer as reducer_module
from research.krb1_c_stress_reducer.model import canonical_json_bytes
from research.krb1_c_stress_reducer.reducer import CANDIDATE_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "krb1_c_stress"
COST_INPUT = FIXTURES / "p0_2_real_tariff_cost_inputs.json"
TICK_INPUT = FIXTURES / "p0_1_standard_stock_tick_tables.json"
REFERENCE_SOURCES = (
    REPO_ROOT / "research" / "krb1_c_stress_reducer" / "__init__.py",
    REPO_ROOT / "research" / "krb1_c_stress_reducer" / "model.py",
    REPO_ROOT / "research" / "krb1_c_stress_reducer" / "reducer.py",
    REPO_ROOT / "research" / "krb1_c_stress_reducer" / "cli.py",
    REPO_ROOT / "scripts" / "krb1_c_stress_reducer.py",
)
INDEPENDENT_SOURCE = REPO_ROOT / "scripts" / "krb1_c_stress_independent_verify.py"


@pytest.fixture(scope="module")
def inputs():
    return load_cost_inputs(COST_INPUT), load_tick_tables(TICK_INPUT)


@pytest.fixture(scope="module")
def reducer_run(inputs):
    return run_reducer(*inputs)


def test_amendment_real_tariff_and_tax_components_are_bound(inputs):
    costs, _ = inputs
    assert costs.rates["KOSPI"].buy_commission_rate_e12 == 150_000_000
    assert costs.rates["KOSPI"].sell_commission_rate_e12 == 150_000_000
    assert costs.rates["KOSPI"].sell_tax_rate_e12 == 2_000_000_000
    assert costs.rates["KOSDAQ"].sell_tax_rate_e12 == 2_000_000_000
    kospi_components = dict(costs.records[0].sell_tax_components)
    assert kospi_components == {
        "KOSPI_SECURITIES_TRANSACTION_TAX": 500_000_000,
        "KOSPI_RURAL_SPECIAL_TAX": 1_500_000_000,
    }
    assert all(record.cost_basis == "REAL_TRADING_TARIFF" for record in costs.records)
    assert all(record.mock_cost_relation == "DIFFERENT" for record in costs.records)


def test_mock_display_rate_cannot_enter_numeric_contract(tmp_path):
    raw = json.loads(COST_INPUT.read_text(encoding="utf-8"))
    raw["market_cost_records"][0]["mock_display_rate_e12"] = 3_500_000_000
    path = tmp_path / "invalid.json"
    path.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(ContractError, match="keys mismatch"):
        load_cost_inputs(path)


def test_current_standard_stock_table_boundaries_and_open_end(inputs):
    _, ticks = inputs
    expected = (
        (2_000, 1, 5),
        (5_000, 5, 10),
        (20_000, 10, 50),
        (50_000, 50, 100),
        (200_000, 100, 500),
        (500_000, 500, 1_000),
    )
    for market in ("KOSPI", "KOSDAQ"):
        table = ticks.markets[market]
        for boundary, below, at in expected:
            assert table.tick(boundary - 1) == below
            assert table.tick(boundary) == at
            assert table.tick(boundary + 1) == at
        assert table.tick(99_999_999) == 1_000
        assert table.tick_ceil(Fraction(500_001)) == 501_000


def test_incomplete_symbol_to_table_mapping_fails_closed(tmp_path):
    raw = json.loads(TICK_INPUT.read_text(encoding="utf-8"))
    raw["symbol_table_mapping_status"] = "INCOMPLETE"
    path = tmp_path / "tick.json"
    path.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(ContractError, match="not COMPLETE"):
        load_tick_tables(path)


def test_exhaustive_candidate_counts_formula_ties_and_targets(inputs, reducer_run):
    costs, _ = inputs
    assert len(reducer_run.candidates) == 8_002
    assert {
        market: summary.candidate_count
        for market, summary in reducer_run.market_summaries.items()
    } == {"KOSPI": 4_001, "KOSDAQ": 4_001}
    assert all(
        summary.first_price == 5_000
        and summary.last_price == 400_000
        and summary.first_price_after_cap == 400_500
        for summary in reducer_run.market_summaries.values()
    )
    assert reducer_run.witness_market == "KOSPI"
    assert all(candidate.target_check_passed for candidate in reducer_run.candidates)

    sample = reducer_run.candidates[0]
    rates = costs.rates[sample.market]
    expected = (
        1 + rates.buy_commission + Fraction(sample.entry_tick, sample.entry_price)
    ) / (1 - rates.sell_commission - rates.sell_tax - sample.rho_exit) - 1
    assert sample.cost_rate == expected
    assert reducer_run.cap >= reducer_run.raw_cost_rate
    assert reducer_run.cap - Fraction(1, 10_000) < reducer_run.raw_cost_rate


def test_core_execution_does_not_call_builtin_float(monkeypatch, inputs):
    def forbidden_float(*_args, **_kwargs):
        raise AssertionError("builtin float was called")

    monkeypatch.setattr(builtins, "float", forbidden_float)
    run = run_reducer(*inputs)
    assert len(run.candidates) == 8_002


def test_sources_have_no_float_literals_conversions_or_decimal_imports():
    for path in (*REFERENCE_SOURCES, INDEPENDENT_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Constant) and isinstance(node.value, float)
            ), f"float literal in {path}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float", (
                    f"float conversion in {path}:{node.lineno}"
                )
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                assert not {"decimal", "numpy"}.intersection(names)

    independent_tree = ast.parse(INDEPENDENT_SOURCE.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(independent_tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        module and module.startswith("research.krb1_c_stress_reducer")
        for module in imported_modules
    )


def test_artifacts_are_byte_deterministic_and_independently_reproduced(
    tmp_path, monkeypatch, inputs, reducer_run
):
    costs, ticks = inputs
    monkeypatch.setattr(
        reducer_module,
        "_verify_sealed_canonicals",
        lambda _parent, _amendment: None,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    fake_parent = tmp_path / "parent.json"
    fake_amendment = tmp_path / "amendment.json"
    write_artifacts(
        reducer_run,
        costs,
        ticks,
        first,
        REPO_ROOT,
        fake_parent,
        fake_amendment,
    )
    write_artifacts(
        reducer_run,
        costs,
        ticks,
        second,
        REPO_ROOT,
        fake_parent,
        fake_amendment,
    )
    artifact_names = (
        "p0_2_cost_inputs.normalized.json",
        "c_stress_candidates.csv",
        "c_stress_reducer_result.json",
    )
    assert {name: (first / name).read_bytes() for name in artifact_names} == {
        name: (second / name).read_bytes() for name in artifact_names
    }
    with pytest.raises(ContractError, match="already exists"):
        write_artifacts(
            reducer_run,
            costs,
            ticks,
            first,
            REPO_ROOT,
            fake_parent,
            fake_amendment,
        )

    with (first / "c_stress_candidates.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == CANDIDATE_FIELDS
        assert sum(1 for _ in reader) == 8_002

    verification_outputs = []
    stdout_values = []
    for index in range(2):
        verification_output = tmp_path / f"independent-{index}.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(INDEPENDENT_SOURCE),
                "--cost-input",
                str(COST_INPUT),
                "--tick-input",
                str(TICK_INPUT),
                "--candidates",
                str(first / "c_stress_candidates.csv"),
                "--result",
                str(first / "c_stress_reducer_result.json"),
                "--repo-root",
                str(REPO_ROOT),
                "--verification-output",
                str(verification_output),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        verification_outputs.append(verification_output.read_bytes())
        stdout_values.append(completed.stdout)
    assert verification_outputs[0] == verification_outputs[1]
    assert stdout_values[0] == stdout_values[1]
    report = json.loads(verification_outputs[0])
    assert report["status"] == "PASS"
    assert report["float_used"] is False
    assert report["candidate_rows_compared"] == 8_002
    assert report["fraction_rows_matched"] == 8_002
    assert report["witness_rows_matched"] == 8_002
    assert report["target_rows_matched"] == 8_002
    assert report["p0_2_completion_hash_created"] is False
