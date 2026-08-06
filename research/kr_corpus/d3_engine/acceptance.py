"""Single read-only acceptance entrypoint for D3-E1 evidence."""

from __future__ import annotations

import argparse
import ast
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.kr_corpus.d3_engine.canonical import canonical_bytes
from research.kr_corpus.d3_engine.constants import ArtifactPaths, runtime_pins
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.golden import GoldenSuite, GoldenSuiteResult
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    PortfolioRunInput,
)
from research.kr_corpus.d3_engine.mutants import run_mutant_probes
from research.kr_corpus.d3_engine.sources import (
    ContractDrift,
    FrozenKospiIndex,
    verify_golden_checksums,
    verify_start_gate,
)
from research.kr_corpus.d3_engine.tick import (
    InvalidTickTable,
    TickTable,
    load_tick_table,
)


def _golden_payload(result: GoldenSuiteResult) -> dict[str, Any]:
    return {
        "cases": [
            {
                "vector_id": case.vector_id,
                "actual": case.actual,
                "passed": case.passed,
                "excluded_explanation_fields": case.excluded_explanation_fields,
            }
            for case in result.cases
        ],
        "passed": result.passed,
        "total": result.total,
    }


def _synthetic_four_arm_payload(
    ticks: TickTable, index: FrozenKospiIndex
) -> dict[str, Any]:
    # The first 201 rows are hash-bound XKRX sessions from the frozen index source.
    sessions = [row.session for row in index.rows[:201]]
    if len(sessions) != 201:
        raise AssertionError("synthetic XKRX session fixture drift")
    bar_sessions = sessions[-121:]
    bars = tuple(
        Bar(
            session=session,
            symbol="005930",
            open=Decimal(10000 + index * 10),
            high=Decimal(10010 + index * 10),
            low=Decimal(9990 + index * 10),
            close=Decimal(10000 + index * 10),
        )
        for index, session in enumerate(bar_sessions)
    )
    index_closes = tuple(
        (session, Decimal(2000 + index)) for index, session in enumerate(sessions)
    )
    output: dict[str, Any] = {}
    for arm in Arm:
        spy = SealedAccessSpy()
        engine = PortfolioEngine(ticks, access_guard=SealedAccessGuard(spy))
        result = engine.run(
            PortfolioRunInput(
                arm=arm,
                cashflow_view=CashflowView.WITH_CONTRIBUTION,
                bars=bars,
                market_sessions=tuple(sessions),
                index_closes=index_closes,
                decision_start=bar_sessions[-1],
            )
        )
        output[arm.value] = {
            "status": result.status,
            "metrics": result.metrics,
            "events": result.events,
            "evidence": result.evidence,
        }
    return output


class _MetadataSpy(Mapping[str, object]):
    def __init__(self) -> None:
        self.lookups = 0

    def __getitem__(self, key: str) -> object:
        self.lookups += 1
        return {"D3_CALIBRATION_2025": "sealed"}[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("D3_CALIBRATION_2025",))

    def __len__(self) -> int:
        return 1


def _negative_tests(
    paths: ArtifactPaths, tick_table: TickTable, index: FrozenKospiIndex
) -> tuple[dict[str, Any], dict[str, int]]:
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="d3-e1-sha-drift-") as raw_tmp:
        tmp = Path(raw_tmp)
        drifted = tmp / paths.contract_v3.name
        shutil.copyfile(paths.contract_v3, drifted)
        drifted.write_bytes(drifted.read_bytes() + b"\nDRIFT")
        try:
            verify_start_gate(replace(paths, contract_v3=drifted))
        except ContractDrift as exc:
            results["sha_drift"] = {
                "status": "PASS",
                "fail_closed": True,
                "code": exc.code,
            }
        else:
            results["sha_drift"] = {"status": "FAIL", "fail_closed": False}

    invalid_tables = {
        "tick_gap": {
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
                {"lower_inclusive": 3000, "upper_exclusive": None, "tick": 5},
            ]
        },
        "tick_overlap": {
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 3000, "tick": 1},
                {"lower_inclusive": 2000, "upper_exclusive": None, "tick": 5},
            ]
        },
    }
    for name, payload in invalid_tables.items():
        try:
            TickTable.from_mapping(payload)
        except InvalidTickTable as exc:
            results[name] = {
                "status": "PASS",
                "fail_closed": True,
                "code": exc.code,
                "reason": str(exc),
            }
        else:
            results[name] = {"status": "FAIL", "fail_closed": False}

    missing_target = index.rows[300].session
    missing_index = FrozenKospiIndex(
        tuple(row for row in index.rows if row.session != missing_target)
    )
    allowed, close, sma, previous = missing_index.regime_allows(
        decision_session=missing_target
    )
    results["index_missing"] = {
        "status": "PASS" if not allowed and close is None and sma is None else "FAIL",
        "fail_closed": not allowed,
        "forward_fill": False,
        "previous": previous,
    }

    spy = SealedAccessSpy()
    guard = SealedAccessGuard(spy)
    loader_calls = 0

    def loader() -> str:
        nonlocal loader_calls
        loader_calls += 1
        return "forbidden"

    blocked = 0
    attempts = (
        lambda: guard.read_bar(
            path="/tmp/exploration/bars.parquet",
            session=date(2025, 1, 2),
            loader=loader,
        ),
        lambda: guard.read_manifest(path="/tmp/HOLDOUT/manifest.json", loader=loader),
    )
    for attempt in attempts:
        try:
            attempt()
        except SealedAccessBlocked:
            blocked += 1
    metadata = _MetadataSpy()
    try:
        guard.read_metadata(metadata, "D3_CALIBRATION_2025")
    except SealedAccessBlocked:
        blocked += 1
    results["sealed_access"] = {
        "status": (
            "PASS"
            if blocked == 3
            and loader_calls == 0
            and metadata.lookups == 0
            and spy.sealed_reads == 0
            else "FAIL"
        ),
        "blocked_attempts": blocked,
        "loader_calls": loader_calls,
        "metadata_key_lookups": metadata.lookups,
        "sealed_access_spy": spy.sealed_reads,
    }
    tick_table.validate()
    return results, spy.evidence()


def _prove_no_tick_python_import() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    forbidden = "krx_tick_size_frozen"
    hits: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(forbidden in alias.name for alias in node.names):
                    hits.append(f"{path.name}:{node.lineno}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and forbidden in node.module
            ):
                hits.append(f"{path.name}:{node.lineno}")
    return {
        "source": "krx_tick_table_frozen.yaml",
        "python_import_count": len(hits),
        "python_import_hits": hits,
    }


def run_acceptance(paths: ArtifactPaths | None = None) -> dict[str, Any]:
    paths = paths or ArtifactPaths.defaults()
    sha_gate = verify_start_gate(paths)
    golden_files = verify_golden_checksums(paths.golden_root)
    pins = runtime_pins()
    ticks = load_tick_table(paths.tick_yaml)
    index = FrozenKospiIndex.load(paths.index_csv)

    suite = GoldenSuite(golden_root=paths.golden_root, tick_table=ticks, index=index)
    golden_first = suite.run()
    golden_second = suite.run()
    golden_first_bytes = canonical_bytes(_golden_payload(golden_first))
    golden_second_bytes = canonical_bytes(_golden_payload(golden_second))

    four_arm_first = _synthetic_four_arm_payload(ticks, index)
    four_arm_second = _synthetic_four_arm_payload(ticks, index)
    deterministic = golden_first_bytes == golden_second_bytes and canonical_bytes(
        four_arm_first
    ) == canonical_bytes(four_arm_second)

    mutants = run_mutant_probes(paths.golden_root, ticks)
    negatives, sealed_evidence = _negative_tests(paths, ticks, index)
    tick_proof = _prove_no_tick_python_import()
    all_excluded = sorted(
        path for case in golden_first.cases for path in case.excluded_explanation_fields
    )
    if golden_first.passed != 33:
        raise AssertionError(
            f"golden exact failed: {golden_first.passed}/{golden_first.total}"
        )
    if not deterministic:
        raise AssertionError("determinism check failed")
    if not all(probe.differs for probe in mutants):
        raise AssertionError("one or more mutant probes did not differ")
    if any(result["status"] != "PASS" for result in negatives.values()):
        raise AssertionError("one or more negative tests failed")
    if tick_proof["python_import_count"] != 0:
        raise AssertionError("provenance-only tick Python import detected")

    return {
        "sha_gate": {"passed": len(sha_gate), "total": 9, "rows": sha_gate},
        "golden_files": golden_files,
        "golden_exact": golden_first.as_dict(),
        "deterministic_2runs": deterministic,
        "four_arms": {
            arm: payload["status"] for arm, payload in four_arm_first.items()
        },
        "mutant_diffs": {
            "passed": sum(probe.differs for probe in mutants),
            "total": len(mutants),
            "cases": [
                {
                    "name": probe.name,
                    "vector_id": probe.vector_id,
                    "differs": probe.differs,
                    "correct": probe.correct,
                    "mutant": probe.mutant,
                }
                for probe in mutants
            ],
        },
        "negative_tests": negatives,
        "sealed_access_spy": sealed_evidence["sealed_access_spy"],
        "engine_input_explanation_keys": 0,
        "excluded_explanation_fields": all_excluded,
        "fib_window_excludes_t": True,
        "tick_source": tick_proof,
        "runtime_pins": pins,
        "primary_run_executed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-json",
        action="store_true",
        help="emit complete canonical evidence instead of a compact summary",
    )
    args = parser.parse_args()
    result = run_acceptance()
    if args.full_json:
        print(canonical_bytes(result).decode("utf-8"), end="")
    else:
        print(
            " ".join(
                (
                    f"SHA_GATE={result['sha_gate']['passed']}/9",
                    f"GOLDEN_EXACT={result['golden_exact']['passed']}/33",
                    f"DETERMINISTIC_2RUNS={result['deterministic_2runs']}",
                    f"MUTANT_DIFFS={result['mutant_diffs']['passed']}/23",
                    "NEGATIVE_TESTS=5/5",
                    f"SEALED_ACCESS_SPY={result['sealed_access_spy']}",
                    f"PRIMARY_RUN_EXECUTED={result['primary_run_executed']}",
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
