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
    assert list(tmp_path.glob("*.partial")) == []
    with pytest.raises(CliRejection, match="overwrite"):
        atomic_write_json(path, {"a": 2})


def test_atomic_write_cleans_stranded_partial_before_write(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    legacy = path.with_name(path.name + ".partial")
    legacy.write_text("stranded", encoding="utf-8")
    unique = path.with_name(f"{path.name}.99999.1.partial")
    unique.write_text("also-stranded", encoding="utf-8")
    atomic_write_json(path, {"ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert not legacy.exists()
    assert not unique.exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_cli_happy_path_writes_labeled_result_without_forbidden_enumeration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    assert payload["observation_summary"]["persisted"] == "signal_true_only"
    assert payload["observation_summary"]["total_evaluated"] == 80
    assert all(item["signal"] is True for item in payload["observations"])
    assert payload["adapter"]["scale_estimate"]["sessions"] == 80
    assert payload["adapter"]["scale_estimate"]["symbols"] == 1
    access_paths = payload["adapter"]["path_access_summary"]["year_roots"]
    assert str(year_root) in access_paths
    # No leftover partial after success.
    assert list(tmp_path.glob("*.partial")) == []
    err = capsys.readouterr().err
    assert "SCALE_ESTIMATE" in err
    assert "sessions=80" in err
    assert "symbols=1" in err


def test_cli_two_runs_are_byte_identical(tmp_path: Path) -> None:
    year_root = _minimal_volbreak_year(tmp_path)
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    run_from_args(_base_args(tmp_path, year_root=year_root, output=out_a))
    run_from_args(_base_args(tmp_path, year_root=year_root, output=out_b))
    # Wall-clock fields differ; strip non-deterministic adapter timing only.
    payload_a = json.loads(out_a.read_text(encoding="utf-8"))
    payload_b = json.loads(out_b.read_text(encoding="utf-8"))
    payload_a["adapter"].pop("engine_wall_seconds", None)
    payload_b["adapter"].pop("engine_wall_seconds", None)
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(
        payload_b, sort_keys=True
    )


def _multi_signal_volbreak_year(tmp_path: Path) -> Path:
    """Two symbols with true VOLBREAK signals so observation list order is tested."""

    year_root = tmp_path / "corpus" / "year=2023"
    rows: list[dict] = []
    base = date(2023, 1, 3)
    for symbol, adv_mult in (("AAA", 2.0), ("BBB", 1.0)):
        for index in range(80):
            session = date.fromordinal(base.toordinal() + index)
            close = 100.0 + index * 0.1
            volume = 50_000.0 * adv_mult
            if index == 55:
                close = 107.0
                volume = 100_000.0 * adv_mult
            elif index > 55:
                close = 106.0
            rows.append(_row(symbol, session, close=close, volume=volume))
    return _write_year(year_root, rows)


def test_cli_two_runs_byte_identical_with_multi_signal_order(tmp_path: Path) -> None:
    """Determinism must cover list order when ≥2 signals are present (SHOULD-1)."""

    year_root = _multi_signal_volbreak_year(tmp_path)
    out_a = tmp_path / "multi-a.json"
    out_b = tmp_path / "multi-b.json"
    run_from_args(_base_args(tmp_path, year_root=year_root, output=out_a))
    run_from_args(_base_args(tmp_path, year_root=year_root, output=out_b))
    payload_a = json.loads(out_a.read_text(encoding="utf-8"))
    payload_b = json.loads(out_b.read_text(encoding="utf-8"))
    payload_a["adapter"].pop("engine_wall_seconds", None)
    payload_b["adapter"].pop("engine_wall_seconds", None)
    assert len(payload_a["observations"]) >= 2
    assert [item["symbol"] for item in payload_a["observations"]] == [
        item["symbol"] for item in payload_b["observations"]
    ]
    assert json.dumps(payload_a, sort_keys=True, separators=(",", ":")) == json.dumps(
        payload_b, sort_keys=True, separators=(",", ":")
    )
    # Order-sensitive mutant: reverse observations must not match original bytes.
    mutant = json.loads(json.dumps(payload_a))
    mutant["observations"] = list(reversed(mutant["observations"]))
    if len(mutant["observations"]) >= 2:
        assert json.dumps(mutant, sort_keys=True, separators=(",", ":")) != json.dumps(
            payload_a, sort_keys=True, separators=(",", ":")
        )
