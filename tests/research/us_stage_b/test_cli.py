"""CLI gate tests: seven hard rejections, atomic write, and access conservation."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.us_stage_b.cli import (
    CliArgs,
    CliRejection,
    atomic_write_json,
    main,
    run_from_args,
)
from research.us_stage_b.registry import US_CANDIDATE_ORDER

from .conftest import FROZEN_YAML

VOLBREAK_ID = US_CANDIDATE_ORDER[2]
VOLBREAK_HASH = "43ff9a4a99ba3717c2d5563aa58c8a482800082c4fa4c41330712c4460848b0f"


def _write_year(
    root: Path,
    rows: list[dict],
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), root / "part-00000.parquet")
    return root


def _row(
    symbol: str,
    session: date,
    *,
    close: float = 100.0,
    volume: float = 50_000.0,
) -> dict:
    return {
        "symbol": symbol,
        "session_date": datetime(session.year, session.month, session.day),
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
    }


def _minimal_volbreak_year(tmp_path: Path) -> Path:
    """Enough sessions for VOLBREAK lookback (55) + hold (10) on one symbol."""

    year_root = tmp_path / "corpus" / "year=2023"
    rows: list[dict] = []
    base = date(2023, 1, 3)
    for index in range(80):
        session = date.fromordinal(base.toordinal() + index)
        close = 100.0 + index * 0.1
        volume = 50_000.0
        if index == 55:
            close = 107.0
            volume = 100_000.0
        elif index > 55:
            close = 106.0
        rows.append(_row("IDX", session, close=close, volume=volume))
    return _write_year(year_root, rows)


def _base_args(
    tmp_path: Path,
    *,
    year_root: Path,
    output: Path | None = None,
    candidate_id: str = VOLBREAK_ID,
    contract_hash: str = VOLBREAK_HASH,
    mutate_input: bool = False,
    exploration_start: date = date(2023, 1, 3),
    exploration_end: date = date(2023, 3, 23),
) -> CliArgs:
    return CliArgs(
        candidate_id=candidate_id,
        contract_hash=contract_hash,
        candidates_yaml=FROZEN_YAML,
        year_roots=(year_root,),
        exploration_start=exploration_start,
        exploration_end=exploration_end,
        output=output or (tmp_path / "out" / "result.json"),
        mutate_input=mutate_input,
    )


def test_cli_rejects_year_root_outside_nine_allowlisted(tmp_path: Path) -> None:
    bad = tmp_path / "year=2015"
    bad.mkdir()
    args = _base_args(tmp_path, year_root=bad)
    with pytest.raises(CliRejection, match="allowlisted|outside"):
        run_from_args(args)


def test_cli_rejects_2025_plus_exploration_end(tmp_path: Path) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    args = _base_args(
        tmp_path,
        year_root=year_root,
        exploration_end=date(2025, 1, 1),
    )
    with pytest.raises(CliRejection, match="2025"):
        run_from_args(args)


def test_cli_rejects_holdout_and_staging_year_roots(tmp_path: Path) -> None:
    holdout = tmp_path / "holdout" / "year=2016"
    holdout.mkdir(parents=True)
    with pytest.raises(CliRejection, match="holdout|staging|forbidden"):
        run_from_args(_base_args(tmp_path, year_root=holdout))

    staging = tmp_path / "_staging" / "year=2017"
    staging.mkdir(parents=True)
    with pytest.raises(CliRejection, match="holdout|staging|forbidden"):
        run_from_args(_base_args(tmp_path, year_root=staging))


def test_cli_rejects_unknown_candidate(tmp_path: Path) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    args = _base_args(
        tmp_path,
        year_root=year_root,
        candidate_id="US-UNKNOWN-CANDIDATE",
        contract_hash=VOLBREAK_HASH,
    )
    with pytest.raises(CliRejection, match="shared fallback|unsupported"):
        run_from_args(args)


def test_cli_rejects_contract_hash_mismatch(tmp_path: Path) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    args = _base_args(
        tmp_path,
        year_root=year_root,
        contract_hash="0" * 64,
    )
    with pytest.raises(CliRejection, match="contract-hash mismatch"):
        run_from_args(args)


def test_cli_rejects_output_overwrite(tmp_path: Path) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    output = tmp_path / "exists.json"
    output.write_text("{}", encoding="utf-8")
    args = _base_args(tmp_path, year_root=year_root, output=output)
    with pytest.raises(CliRejection, match="overwrite"):
        run_from_args(args)


def test_cli_rejects_input_mutation_request(tmp_path: Path) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    args = _base_args(tmp_path, year_root=year_root, mutate_input=True)
    with pytest.raises(CliRejection, match="input mutation"):
        run_from_args(args)


def test_cli_main_exit_code_for_rejection(tmp_path: Path) -> None:
    bad = tmp_path / "year=2010"
    bad.mkdir()
    code = main(
        [
            "--candidate-id",
            VOLBREAK_ID,
            "--contract-hash",
            VOLBREAK_HASH,
            "--candidates-yaml",
            str(FROZEN_YAML),
            "--year-root",
            str(bad),
            "--exploration-start",
            "2023-01-03",
            "--exploration-end",
            "2023-03-23",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 2


def test_atomic_write_refuses_overwrite_and_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert not path.with_name(path.name + ".partial").exists()
    with pytest.raises(CliRejection, match="overwrite"):
        atomic_write_json(path, {"a": 2})


def test_cli_happy_path_writes_labeled_result_without_forbidden_enumeration(
    tmp_path: Path,
) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    # Plant forbidden siblings that must never appear in access records.
    (tmp_path / "corpus" / "holdout").mkdir()
    (tmp_path / "corpus" / "_staging").mkdir()
    output = tmp_path / "run.json"
    args = _base_args(tmp_path, year_root=year_root, output=output)
    written = run_from_args(args)
    assert written == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["strategy_id"] == VOLBREAK_ID
    assert payload["contract_hash"] == VOLBREAK_HASH
    assert "EXPLORATORY_FALSIFICATION_ONLY" in payload["labels"]
    assert "SURVIVORSHIP_BIASED=TRUE" in payload["labels"]
    assert "VOLUME_CA_UNRESOLVED" in payload["labels"]
    assert payload["orders"] == 0
    assert payload["adapter"]["HOLDOUT_READS"] == 0
    assert payload["adapter"]["FORBIDDEN_ROOT_ENUMERATIONS"] == 0
    assert payload["adapter"]["ORDERS"] == 0
    assert payload["adapter"]["ACCOUNT_CONTACT"] == 0
    assert payload["adapter"]["DB_WRITES"] == 0
    access_paths = payload["adapter"]["path_access_summary"]["year_roots"]
    assert str(year_root) in access_paths
    # No leftover partial after success.
    assert not output.with_name(output.name + ".partial").exists()
