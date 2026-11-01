"""End-to-end on synthetic bars. Each test skips without LightGBM, so CI skips them.

Run in the research environment:

    PYTHONPATH=. "$ROOT/venv/bin/python" -m pytest --noconftest -c /dev/null \
      tests/research/kr_boosting/test_kr_boosting_pipeline.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import pickle
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from research.eval_contract.contract import BRACKET_CONSERVATIVE, sha256_canonical
from research.kr_boosting import config as cfg
from research.kr_boosting import pipeline as pl
from research.kr_boosting import report as report_module
from research.kr_boosting import run
from research.kr_boosting.config import CONFIG_GRID, HYP_ADMIT, HYP_ENTRY
from research.kr_boosting.exposure_audit import Hit
from research.kr_boosting.panel import (
    ExportIdentity,
    ExportIdentityError,
    build_panel,
)
from research.kr_boosting.report import write_report
from tests.research.kr_boosting.helpers import (
    flat_rows,
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
    assert out["grid_sha256"] == cfg.grid_sha256(GRID)
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


def _fixture_registry(
    path: Path, *, parquet: str = "aa" * 32, manifest: str = "bb" * 32
) -> Path:
    """A registry whose manifests pin the given dataset; returns the repo path."""

    experiments = path / "research/experiments"
    (experiments / "manifests").mkdir(parents=True, exist_ok=True)
    hashes = {
        "dataset_parquet_sha256_kr": parquet,
        "dataset_manifest_sha256_kr": manifest,
    }
    entries = []
    for h in (HYP_ADMIT, HYP_ENTRY):
        relative = f"research/experiments/manifests/{h}.json"
        (path / relative).write_text(json.dumps({"hashes": hashes}))
        entries.append(
            {
                "id": h,
                "status": "draft",
                "manifest": relative,
                "manifest_sha256": "ab" * 32,
                "previous_manifest_sha256": [],
                "k": 1,
            }
        )
    (experiments / "registry.json").write_text(json.dumps({"hypotheses": entries}))
    return path


def _fixture_repo(path: Path) -> Path:
    _fixture_registry(path)
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
    parquet = tmp_path / "export.parquet"
    whole.to_parquet(parquet)
    run.diagnostic(parquet, root=root, identity=identity)
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    with pytest.raises(FileExistsError):
        run.diagnostic(parquet, root=root, identity=identity)

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
    config = sha256_canonical(
        {"policy": pl.POL_GBM, "label": "validate", "grid": cfg.grid_sha256(GRID)}
    )
    rendered = root / "contract_reports/validate" / f"{HYP_ADMIT}.{pl.POL_GBM}.md"
    assert f"config sha256: {config}" in rendered.read_text()


# ------------------------------------------- the sealed read, no LightGBM needed


class _SealedRead(Exception):
    pass


IDENTITY = ExportIdentity("aa" * 32, "bb" * 32, 1)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The CLI with a counted row reader; prerequisites are in place."""

    root = tmp_path / "run"
    root.mkdir()
    bundle = pickle.dumps({})
    (root / "frozen_models.pkl").write_bytes(bundle)
    (root / "validate_results.json").write_text(
        json.dumps({"frozen_bundle_sha256": hashlib.sha256(bundle).hexdigest()})
    )
    repo = _fixture_registry(tmp_path / "repo")
    reads: list[Path] = []

    def read_rows(path: Path, **_kwargs):
        reads.append(path)
        assert (root / run.DIAGNOSTIC_CLAIM).is_file(), "read before the claim"
        raise _SealedRead

    monkeypatch.setattr(run, "verify_export", lambda _d: IDENTITY)
    monkeypatch.setattr(run, "read_rows", read_rows)

    def main(stage: str = "diagnostic") -> int:
        args = [stage, "--export-dir", str(tmp_path), "--root", str(root)]
        return run.main([*args, "--repo", str(repo), "--report", str(tmp_path / "r")])

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


def test_a_direct_call_claims_before_it_reads_and_blocks_another(
    cli, tmp_path: Path
) -> None:
    _, root, reads = cli
    parquet = tmp_path / "export.parquet"
    with pytest.raises(_SealedRead):  # the reader itself asserts the claim is on disk
        run.diagnostic(parquet, root=root, identity=IDENTITY)
    assert reads == [parquet]
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    with pytest.raises(FileExistsError, match="Count another look"):
        run.diagnostic(parquet, root=root, identity=IDENTITY)
    assert reads == [parquet]


def test_a_crash_after_the_read_leaves_the_claim_and_no_result(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = cli
    parquet = tmp_path / "export.parquet"
    reads: list[Path] = []

    def read_rows(path: Path, **_kwargs):
        reads.append(path)
        return frame(flat_rows("000001", weekdays(date(2023, 1, 2), 3)))

    class _Crash(Exception):
        pass

    def compute_features(_panel):
        raise _Crash

    monkeypatch.setattr(run, "read_rows", read_rows)
    monkeypatch.setattr(run, "compute_features", compute_features)
    with pytest.raises(_Crash):
        run.diagnostic(parquet, root=root, identity=IDENTITY)
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    assert not (root / "diagnostic_results.json").exists()
    with pytest.raises(FileExistsError, match="Count another look"):
        run.diagnostic(parquet, root=root, identity=IDENTITY)
    assert reads == [parquet]


def test_only_diagnostic_may_ask_for_sealed_rows() -> None:
    package = Path(run.__file__).parent
    callers: list[tuple[str, str]] = []
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and any(
                    k.arg == "allow_sealed" for k in node.keywords
                ):
                    callers.append((source.name, function.name))
    assert callers == [("run.py", "diagnostic")]


@pytest.mark.parametrize(
    "name", [run.DIAGNOSTIC_CLAIM, "diagnostic_results.json", "diagnostic_outputs.pkl"]
)
def test_validate_refuses_a_root_with_a_diagnostic_read(cli, name: str) -> None:
    main, root, reads = cli
    (root / name).write_text("{}")
    bundle = (root / "frozen_models.pkl").read_bytes()
    with pytest.raises(FileExistsError, match="use a new --root"):
        main("validate")
    assert reads == []
    panel = build_panel(frame(flat_rows("000001", weekdays(date(2019, 1, 1), 3))))
    with pytest.raises(FileExistsError, match="use a new --root"):
        run.validate(panel, root=root, identity=None)
    assert (root / "frozen_models.pkl").read_bytes() == bundle


@pytest.mark.parametrize("stage", ["validate", "diagnostic", "report"])
@pytest.mark.parametrize(
    "pinned", [{"parquet": "cc" * 32}, {"manifest": "cc" * 32}], ids=["parquet", "doc"]
)
def test_an_unregistered_export_is_refused_before_any_read(
    cli, tmp_path: Path, stage: str, pinned: dict[str, str]
) -> None:
    main, root, reads = cli
    _fixture_registry(tmp_path / "repo", **pinned)
    with pytest.raises(ExportIdentityError, match="is not the dataset"):
        main(stage)
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def _same_run() -> tuple[dict, dict, dict]:
    export = {"parquet_sha256": "aa" * 32, "manifest_sha256": "bb" * 32, "rows": 1}
    results = {
        "registry": {HYP_ADMIT: "ab" * 32, HYP_ENTRY: "ab" * 32},
        "frozen_bundle_sha256": "ee" * 32,
        "export": export,
    }
    diag = {"frozen_bundle_sha256": "ee" * 32, "export": dict(export)}
    entries = {h: {"manifest_sha256": "ab" * 32} for h in (HYP_ADMIT, HYP_ENTRY)}
    return results, diag, entries


def test_report_accepts_one_run() -> None:
    results, diag, entries = _same_run()
    report_module._require_same_run(results, diag, entries, IDENTITY)


def test_report_refuses_a_manifest_changed_after_validate() -> None:
    results, diag, entries = _same_run()
    entries[HYP_ENTRY]["manifest_sha256"] = "cd" * 32
    with pytest.raises(ValueError, match="manifest changed after validate"):
        report_module._require_same_run(results, diag, entries, IDENTITY)


def test_report_refuses_a_diagnostic_of_another_bundle() -> None:
    results, diag, entries = _same_run()
    diag["frozen_bundle_sha256"] = "ff" * 32
    with pytest.raises(ValueError, match="different frozen bundle"):
        report_module._require_same_run(results, diag, entries, IDENTITY)


def test_write_report_checks_the_run_before_it_renders(tmp_path: Path) -> None:
    results, diag, _ = _same_run()
    results["registry"][HYP_ADMIT] = "cd" * 32  # the fixture registry says "ab"
    root = tmp_path / "run"
    root.mkdir()
    (root / "validate_results.json").write_text(json.dumps(results))
    (root / "diagnostic_results.json").write_text(json.dumps(diag))
    for name in ("validate_outputs.pkl", "diagnostic_outputs.pkl"):
        (root / name).write_bytes(pickle.dumps({}))
    out = tmp_path / "report.md"
    with pytest.raises(ValueError, match="manifest changed after validate"):
        write_report(
            root=root,
            repo=_fixture_registry(tmp_path / "repo"),
            out=out,
            identity=IDENTITY,
            export_dir=tmp_path,
        )
    assert not out.exists()


@pytest.mark.parametrize("stage", ["results", "diag", "report"])
def test_report_refuses_mixed_exports(stage: str) -> None:
    results, diag, entries = _same_run()
    identity = IDENTITY
    if stage == "results":
        results["export"] = {**results["export"], "rows": 2}
    elif stage == "diag":
        diag["export"] = {**diag["export"], "rows": 2}
    else:
        identity = ExportIdentity("aa" * 32, "bb" * 32, 2)
    with pytest.raises(ValueError, match="different exports"):
        report_module._require_same_run(results, diag, entries, identity)


def test_grid_hash_follows_the_grid_and_keeps_the_registered_default() -> None:
    assert cfg.grid_sha256() == (
        "e112e9e51cd48cf6c10f5b9577696a3977d46a1741dcd015e214dcaf4054ae76"
    )
    assert cfg.grid_sha256(cfg.CONFIG_GRID) == cfg.grid_sha256()
    assert cfg.grid_sha256(GRID) != cfg.grid_sha256()
