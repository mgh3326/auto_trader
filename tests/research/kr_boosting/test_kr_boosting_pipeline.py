"""End-to-end on synthetic bars. Each test skips without LightGBM, so CI skips them.

Run in the research environment:

    PYTHONPATH=. "$ROOT/venv/bin/python" -m pytest --noconftest -c /dev/null \
      tests/research/kr_boosting/test_kr_boosting_pipeline.py
"""

from __future__ import annotations

import hashlib
import json
import pickle
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from research.eval_contract.contract import BRACKET_CONSERVATIVE
from research.kr_boosting import pipeline as pl
from research.kr_boosting import report as report_module
from research.kr_boosting import run
from research.kr_boosting.config import CONFIG_GRID, HYP_ADMIT, HYP_ENTRY
from research.kr_boosting.exposure_audit import Hit
from research.kr_boosting.panel import ExportIdentity, build_panel
from research.kr_boosting.report import write_report
from tests.research.kr_boosting.helpers import (
    frame,
    random_walk_rows,
    weekdays,
)

GRID = tuple({**c, "n_estimators": 5, "min_data_in_leaf": 5} for c in CONFIG_GRID[:3])
SEEDS = (1, 2)


@pytest.fixture(scope="module")
def panel():
    pytest.importorskip("lightgbm")
    sessions = weekdays(date(2015, 1, 1), 1150)  # through 2019
    rows: list[dict[str, object]] = []
    for i in range(14):
        rows += random_walk_rows(f"0000{i:02d}", sessions, 100 + i)
    return build_panel(frame(rows))


@pytest.fixture(scope="module")
def result(panel, tmp_path_factory):
    root = tmp_path_factory.mktemp("run")
    out = run.validate(
        panel,
        root=root,
        identity=None,
        years=(2018,),
        seal_boundary=date(2019, 1, 1),
        frozen_year=2019,
        grid=GRID,
        seeds=SEEDS,
    )
    return out, root


def test_both_hypotheses_get_a_verdict_and_every_policy_a_bracket(result) -> None:
    out, _ = result
    for hyp in (HYP_ADMIT, HYP_ENTRY):
        section = out[hyp]
        assert section["verdict"]["verdict"] in {"CONTINUE_TO_U8", "STOP"}
        assert section["rows"]["excluded_no_available_at"] == 0
        for runs in section["policies"].values():
            assert runs[BRACKET_CONSERVATIVE]["status"] == "ok"
            assert runs["optimistic_touch"]["status"] == "ok"
            assert runs["volume_share"]["status"] == "ok"
            assert runs["empirical"]["status"] == "unavailable"


def test_cash_control_is_exactly_flat(result) -> None:
    out, _ = result
    for hyp in (HYP_ADMIT, HYP_ENTRY):
        row = out[hyp]["policies"][pl.POL_CASH][BRACKET_CONSERVATIVE]
        assert row["net_vs_cash_total"] == 0
        assert row["entries"] == 0


def test_search_stays_inside_the_declared_budget(result) -> None:
    out, _ = result
    for hyp in (HYP_ADMIT, HYP_ENTRY):
        for fold in out[hyp]["folds"]:
            assert fold["configs_tried"] == len(GRID)
            assert len(fold["finalists"]) <= 3
            assert all(len(seeds) == len(SEEDS) for seeds in fold["fitted_sha256"])


def test_frozen_models_are_written_with_a_hash(result) -> None:
    out, root = result
    assert (root / "frozen_models.pkl").is_file()
    with (root / "frozen_models.pkl").open("rb") as handle:
        frozen = pickle.load(handle)
    assert set(frozen) == {"admit_quantile", "touch", "xret"}
    assert len(out["frozen_bundle_sha256"]) == 64
    assert json.loads((root / "validate_results.json").read_text())["frozen"]


def test_same_inputs_give_the_same_models(panel, tmp_path: Path, result) -> None:
    first, _ = result
    again = run.validate(
        panel,
        root=tmp_path,
        identity=None,
        years=(2018,),
        seal_boundary=date(2019, 1, 1),
        frozen_year=2019,
        grid=GRID,
        seeds=SEEDS,
    )
    assert again["frozen"] == first["frozen"]
    assert again[HYP_ADMIT]["policies"] == first[HYP_ADMIT]["policies"]


def test_year_masks_keep_purge_and_gap_for_training_rows(panel) -> None:
    t = np.arange(len(panel.calendar))
    masks = pl._masks(panel.calendar, t, 2018, purge=5, gap=5)
    boundary = next(i for i, d in enumerate(panel.calendar) if d >= date(2018, 1, 1))
    assert t[masks["train"]].max() == boundary - 11
    inner_boundary = next(
        i for i, d in enumerate(panel.calendar) if d >= date(2017, 1, 1)
    )
    assert t[masks["fit"]].max() == inner_boundary - 11
    assert t[masks["inner"]].min() == inner_boundary
    assert t[masks["inner"]].max() == boundary - 11


def _fixture_repo(path: Path) -> Path:
    registry = path / "research/experiments/registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "hypotheses": [
                    {
                        "id": h,
                        "status": "draft",
                        "manifest_sha256": "ab" * 32,
                        "previous_manifest_sha256": [],
                        "k": 1,
                    }
                    for h in (HYP_ADMIT, HYP_ENTRY)
                ]
            }
        )
    )
    git = [
        "git",
        "-C",
        str(path),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
    ]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)
    return path


def _fixture_export(path: Path) -> Path:
    path.mkdir()
    table = path / "delist_resolution.v1.csv"
    table.write_text("symbol,delist_reason\n000001,해산\n")
    digest = hashlib.sha256(table.read_bytes()).hexdigest()
    (path / "manifest.json").write_text(
        json.dumps({"files": {"delist_resolution": {"sha256": digest}}})
    )
    return path


def test_validate_diagnostic_and_report_run_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("lightgbm")
    # The fixture repository holds no evidence; the report refuses that on purpose.
    monkeypatch.setattr(
        report_module,
        "run_audit",
        lambda _root: (Hit("fixture.md", 1, "fixture evidence"),),
    )
    sessions = weekdays(date(2015, 1, 1), 2600)  # through 2024
    rows: list[dict[str, object]] = []
    for i in range(14):
        rows += random_walk_rows(f"0000{i:02d}", sessions, 100 + i)
    whole = frame(rows)
    identity = ExportIdentity("aa" * 32, "bb" * 32, len(whole))
    repo = _fixture_repo(tmp_path / "repo")
    root = tmp_path / "run"

    run.validate(
        build_panel(whole[whole["session"] < date(2020, 1, 1)]),
        root=root,
        identity=identity,
        years=(2018,),
        seal_boundary=date(2019, 1, 1),
        frozen_year=2019,
        grid=GRID,
        seeds=SEEDS,
        repo=repo,
    )
    run.diagnostic(build_panel(whole), root=root, identity=identity)
    with pytest.raises(FileExistsError):
        run.diagnostic(build_panel(whole), root=root, identity=identity)

    out = tmp_path / "report.md"
    write_report(
        root=root,
        repo=repo,
        out=out,
        identity=identity,
        export_dir=_fixture_export(tmp_path / "export"),
    )
    text = out.read_text()
    assert "## Verdict" in text
    assert "## Payout sensitivity of the terminal labels" in text
    assert text.count("Experiment-Manifest-Sha256:") == 2
    assert text.count("Experiment-Test-Look:") == 1


# ------------------------------------------- the sealed read, no LightGBM needed


class _SealedRead(Exception):
    pass


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The diagnostic CLI with a counted row reader; prerequisites are in place."""

    root = tmp_path / "run"
    root.mkdir()
    bundle = root / "frozen_models.pkl"
    bundle.write_bytes(b"frozen")
    (root / "validate_results.json").write_text(
        json.dumps({"frozen_bundle_sha256": hashlib.sha256(b"frozen").hexdigest()})
    )
    reads: list[Path] = []

    def read_rows(path: Path, **_kwargs):
        reads.append(path)
        raise _SealedRead

    monkeypatch.setattr(
        run, "verify_export", lambda _d: ExportIdentity("aa" * 32, "bb" * 32, 1)
    )
    monkeypatch.setattr(run, "read_rows", read_rows)

    def main() -> int:
        return run.main(
            ["diagnostic", "--export-dir", str(tmp_path), "--root", str(root)]
        )

    return main, root, reads


def test_no_sealed_row_is_read_without_a_frozen_bundle(cli) -> None:
    main, root, reads = cli
    (root / "frozen_models.pkl").unlink()
    with pytest.raises(FileNotFoundError):
        main()
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def test_no_sealed_row_is_read_with_a_changed_bundle(cli) -> None:
    main, root, reads = cli
    (root / "frozen_models.pkl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed after validate"):
        main()
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def test_no_sealed_row_is_read_after_a_finished_diagnostic(cli) -> None:
    main, root, reads = cli
    (root / "diagnostic_results.json").write_text("{}")
    with pytest.raises(FileExistsError):
        main()
    assert reads == []


def test_a_crashed_read_stays_recorded_and_blocks_another(cli) -> None:
    main, root, reads = cli
    with pytest.raises(_SealedRead):
        main()
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    with pytest.raises(FileExistsError):
        main()
    assert len(reads) == 1
