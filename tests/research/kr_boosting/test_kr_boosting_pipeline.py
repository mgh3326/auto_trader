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
import pandas as pd
import pytest

from research.eval_contract.contract import BRACKET_CONSERVATIVE, sha256_canonical
from research.kr_boosting import config as cfg
from research.kr_boosting import pipeline as pl
from research.kr_boosting import report as report_module
from research.kr_boosting import run
from research.kr_boosting.artifacts import (
    MANIFEST_NAME,
    ArtifactRefused,
    load_artifact,
    write_artifact,
)
from research.kr_boosting.config import CONFIG_GRID, HYP_ADMIT, HYP_ENTRY
from research.kr_boosting.exposure_audit import Hit
from research.kr_boosting.panel import (
    PARQUET_NAME,
    ExportIdentity,
    ExportIdentityError,
    SealedPeriodRead,
    build_panel,
    sha256_file,
    verify_export,
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


def _fixture_repo(path: Path, **pinned: str) -> Path:
    _fixture_registry(path, **pinned)
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


def _fixture_export(path: Path, whole: pd.DataFrame) -> Path:
    path.mkdir()
    table = path / "delist_resolution.v1.csv"
    table.write_text("symbol,delist_reason\n000001,해산\n")
    parquet = path / PARQUET_NAME
    whole.to_parquet(parquet)
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "files": {
                    "delist_resolution": {"sha256": sha256_file(table)},
                    "kr_daily_causal": {
                        "sha256": sha256_file(parquet),
                        "rows": len(whole),
                    },
                }
            }
        )
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
    export = _fixture_export(tmp_path / "export", whole)
    identity = verify_export(export)
    repo = _fixture_repo(
        tmp_path / "repo",
        parquet=identity.parquet_sha256,
        manifest=identity.manifest_sha256,
    )
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
    run.diagnostic(export, root=root, repo=repo)
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    with pytest.raises(FileExistsError):
        run.diagnostic(export, root=root, repo=repo)

    out = tmp_path / "report.md"
    write_report(
        root=root,
        repo=repo,
        out=out,
        identity=identity,
        export_dir=export,
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
    digest = write_artifact(root, run.FROZEN_BUNDLE, {})
    (root / "validate_results.json").write_text(
        json.dumps({"frozen_bundle_sha256": digest})
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

    return main, root, reads, repo


def test_no_sealed_row_is_read_without_a_frozen_bundle(cli) -> None:
    main, root, reads, _ = cli
    (root / "frozen_models.pkl").unlink()
    with pytest.raises(FileNotFoundError):
        main()
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def test_no_sealed_row_is_read_with_a_changed_bundle(cli) -> None:
    main, root, reads, _ = cli
    (root / "frozen_models.pkl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed after validate"):
        main()
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def test_no_sealed_row_is_read_after_a_finished_diagnostic(cli) -> None:
    main, root, reads, _ = cli
    (root / "diagnostic_results.json").write_text("{}")
    with pytest.raises(FileExistsError):
        main()
    assert reads == []


def test_a_crashed_read_stays_recorded_and_blocks_another(cli) -> None:
    main, root, reads, _ = cli
    with pytest.raises(_SealedRead):
        main()
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    with pytest.raises(FileExistsError):
        main()
    assert len(reads) == 1


def test_a_direct_call_claims_before_it_reads_and_blocks_another(
    cli, tmp_path: Path
) -> None:
    _, root, reads, repo = cli
    parquet = tmp_path / PARQUET_NAME
    with pytest.raises(_SealedRead):  # the reader itself asserts the claim is on disk
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert reads == [parquet]
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    with pytest.raises(FileExistsError, match="Count another look"):
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert reads == [parquet]


def test_a_crash_after_the_read_leaves_the_claim_and_no_result(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _, repo = cli
    parquet = tmp_path / PARQUET_NAME
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
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert (root / run.DIAGNOSTIC_CLAIM).is_file()
    assert not (root / "diagnostic_results.json").exists()
    with pytest.raises(FileExistsError, match="Count another look"):
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert reads == [parquet]


def test_a_direct_call_on_an_unregistered_export_uses_no_look(
    cli, tmp_path: Path
) -> None:
    _, root, reads, repo = cli
    _fixture_registry(repo, parquet="cc" * 32)
    with pytest.raises(ExportIdentityError, match="is not the dataset"):
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def test_a_direct_call_on_a_changed_parquet_uses_no_look(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, reads, repo = cli

    def changed(_directory: Path):
        raise ExportIdentityError("sha256 mismatch")

    monkeypatch.setattr(run, "verify_export", changed)
    with pytest.raises(ExportIdentityError, match="sha256 mismatch"):
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


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
    main, root, reads, _ = cli
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
    main, root, reads, repo = cli
    _fixture_registry(repo, **pinned)
    with pytest.raises(ExportIdentityError, match="is not the dataset"):
        main(stage)
    assert reads == []
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


class _ComputedFeatures(Exception):
    pass


def _validate_panel_ending(
    last: date, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **window: object
) -> None:
    def compute_features(_panel):
        raise _ComputedFeatures

    monkeypatch.setattr(run, "compute_features", compute_features)
    sessions = [d for d in weekdays(date(2022, 12, 26), 10) if d <= last]
    panel = build_panel(frame(flat_rows("000001", sessions)))
    assert panel.calendar[-1] == last
    run.validate(panel, root=tmp_path / "run", identity=None, **window)


@pytest.mark.parametrize(
    "window",
    [
        {},
        {
            "years": (2023, 2024),
            "seal_boundary": date(2025, 1, 1),
            "frozen_year": 2025,
        },
    ],
    ids=["defaults", "window-moved-over-the-seal"],
)
def test_validate_refuses_a_panel_with_a_sealed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: dict[str, object]
) -> None:
    with pytest.raises(SealedPeriodRead, match="panel ends 2023-01-02"):
        _validate_panel_ending(date(2023, 1, 2), tmp_path, monkeypatch, **window)
    assert not (tmp_path / "run").exists()


def test_validate_accepts_a_panel_ending_on_the_last_validation_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run.VALIDATE_LAST == date(2022, 12, 31)
    with pytest.raises(_ComputedFeatures):
        _validate_panel_ending(date(2022, 12, 30), tmp_path, monkeypatch)


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


class _Payload:
    """Unpickling this writes a file, standing in for arbitrary code."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self):
        return (Path.write_text, (self.marker, "executed"))


def _plant_payload(root: Path, name: str, path: Path) -> Path:
    marker = path / "payload-ran"
    (root / name).write_bytes(pickle.dumps(_Payload(marker)))
    return marker


def _report_root(path: Path) -> Path:
    results, diag, _ = _same_run()
    root = path / "run"
    root.mkdir()
    (root / "validate_results.json").write_text(json.dumps(results))
    (root / "diagnostic_results.json").write_text(json.dumps(diag))
    return root


def _write_report(path: Path, root: Path) -> Path:
    out = path / "report.md"
    write_report(
        root=root,
        repo=_fixture_registry(path / "repo"),
        out=out,
        identity=IDENTITY,
        export_dir=path,
    )
    return out


def test_write_report_checks_the_run_before_it_loads_an_artifact(
    tmp_path: Path,
) -> None:
    root = _report_root(tmp_path)
    results = json.loads((root / "validate_results.json").read_text())
    results["registry"][HYP_ADMIT] = "cd" * 32  # the fixture registry says "ab"
    (root / "validate_results.json").write_text(json.dumps(results))
    marker = _plant_payload(root, "validate_outputs.pkl", tmp_path)
    with pytest.raises(ValueError, match="manifest changed after validate"):
        _write_report(tmp_path, root)
    assert not marker.exists()
    assert not (tmp_path / "report.md").exists()


def test_the_payload_runs_when_nothing_checks_it(tmp_path: Path) -> None:
    marker = _plant_payload(tmp_path, "x.pkl", tmp_path)
    pickle.loads((tmp_path / "x.pkl").read_bytes())
    assert marker.read_text() == "executed"


@pytest.mark.parametrize("name", ["validate_outputs.pkl", "diagnostic_outputs.pkl"])
def test_write_report_refuses_a_swapped_artifact_without_running_it(
    tmp_path: Path, name: str
) -> None:
    root = _report_root(tmp_path)
    for each in ("validate_outputs.pkl", "diagnostic_outputs.pkl"):
        write_artifact(root, each, {})
    marker = _plant_payload(root, name, tmp_path)
    with pytest.raises(ArtifactRefused, match="!= recorded"):
        _write_report(tmp_path, root)
    assert not marker.exists()
    assert not (tmp_path / "report.md").exists()


def test_write_report_refuses_an_artifact_with_no_recorded_hash(
    tmp_path: Path,
) -> None:
    root = _report_root(tmp_path)
    marker = _plant_payload(root, "validate_outputs.pkl", tmp_path)
    with pytest.raises(ArtifactRefused, match="no hash"):
        _write_report(tmp_path, root)
    assert not marker.exists()


def test_a_swapped_bundle_is_refused_before_the_look(cli, tmp_path: Path) -> None:
    main, root, reads, _ = cli
    marker = _plant_payload(root, run.FROZEN_BUNDLE, tmp_path)
    digest = hashlib.sha256((root / run.FROZEN_BUNDLE).read_bytes()).hexdigest()
    (root / "validate_results.json").write_text(
        json.dumps({"frozen_bundle_sha256": digest})
    )  # results rewritten too; the manifest still disagrees
    with pytest.raises(ArtifactRefused, match="!= recorded"):
        main()
    assert reads == []
    assert not marker.exists()
    assert not (root / run.DIAGNOSTIC_CLAIM).exists()


def test_a_bundle_swapped_after_the_claim_is_still_not_unpickled(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _, repo = cli
    marker = tmp_path / "payload-ran"

    def read_rows(_path: Path, **_kwargs):
        _plant_payload(root, run.FROZEN_BUNDLE, tmp_path)
        return frame(flat_rows("000001", weekdays(date(2023, 1, 2), 3)))

    monkeypatch.setattr(run, "read_rows", read_rows)
    with pytest.raises(ArtifactRefused, match="!= recorded"):
        run.diagnostic(tmp_path, root=root, repo=repo)
    assert not marker.exists()


def test_an_artifact_round_trips_and_a_broken_manifest_refuses(tmp_path: Path) -> None:
    digest = write_artifact(tmp_path, "a.pkl", {"x": 1})
    write_artifact(tmp_path, "b.pkl", [2])
    assert load_artifact(tmp_path, "a.pkl") == {"x": 1}
    assert load_artifact(tmp_path, "b.pkl") == [2]
    assert digest == hashlib.sha256((tmp_path / "a.pkl").read_bytes()).hexdigest()
    (tmp_path / MANIFEST_NAME).write_text("not json")
    with pytest.raises(ArtifactRefused, match="not valid JSON"):
        load_artifact(tmp_path, "a.pkl")
    (tmp_path / MANIFEST_NAME).write_text("[]")
    with pytest.raises(ArtifactRefused, match="not an object"):
        load_artifact(tmp_path, "a.pkl")


def test_only_the_artifact_module_imports_pickle() -> None:
    package = Path(run.__file__).parent
    users = []
    for source in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if "pickle" in names:
                users.append(source.name)
    assert users == ["artifacts.py"]


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
