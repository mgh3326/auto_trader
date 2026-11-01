"""ROB-1429 experiment registry lint: schema, registered report hashes, k."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.ci import experiment_registry_lint as lint

REPO = Path(__file__).resolve().parents[2]
REQUIRED_IDS = ("ROB-1411", "H-entry-price", "H-admit")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway checkout holding a copy of the real registry files."""

    shutil.copytree(REPO / "research/experiments", tmp_path / "research/experiments")
    return tmp_path


def _registry(root: Path) -> dict:
    return json.loads((root / lint.REGISTRY_PATH).read_text())


def _write_registry(root: Path, registry: dict) -> None:
    (root / lint.REGISTRY_PATH).write_text(json.dumps(registry, indent=2))


def _hyp(registry: dict, hid: str) -> dict:
    return next(h for h in registry["hypotheses"] if h["id"] == hid)


def _file_sha(root: Path, rel: str) -> str:
    return hashlib.sha256((root / rel).read_bytes()).hexdigest()


def _write_report(root: Path, rel: str, *, sha: str, look: str | None = None) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# report", "", f"Experiment-Manifest-Sha256: {sha}"]
    if look is not None:
        lines.append(f"Experiment-Test-Look: {look}")
    path.write_text("\n".join(lines) + "\n")
    return rel


def test_repository_registry_passes() -> None:
    assert lint.run(REPO) == []


def test_registry_holds_required_hypotheses_with_k_zero() -> None:
    registry = _registry(REPO)
    by_id = {h["id"]: h for h in registry["hypotheses"]}
    for hid in REQUIRED_IDS:
        assert by_id[hid]["k"] == 0
        assert by_id[hid]["test_looks"] == []


def test_copied_registry_passes(repo: Path) -> None:
    assert lint.run(repo) == []


def test_report_citing_unregistered_hash_fails(repo: Path) -> None:
    _write_report(repo, "research/some_study/REPORT.md", sha="ab" * 32)
    problems = lint.run(repo)
    assert any("cites unregistered manifest hash" in p for p in problems)
    assert lint.main(["--root", str(repo)]) == 1


def test_report_citing_registered_hash_passes(repo: Path) -> None:
    sha = _hyp(_registry(repo), "H-admit")["manifest_sha256"]
    _write_report(repo, "docs/research/h-admit-note.md", sha=sha, look="no")
    assert lint.run(repo) == []
    assert lint.main(["--root", str(repo)]) == 0


def test_file_in_reports_dir_must_cite(repo: Path) -> None:
    path = repo / lint.REPORTS_DIR / "uncited.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a report without a citation\n")
    assert any("must cite" in p for p in lint.run(repo))


def test_test_look_not_counted_in_registry_fails(repo: Path) -> None:
    sha = _hyp(_registry(repo), "H-admit")["manifest_sha256"]
    _write_report(
        repo, "research/experiments/reports/h-admit-look1.md", sha=sha, look="yes"
    )
    problems = lint.run(repo)
    assert any("does not count" in p for p in problems)


def test_test_look_counted_with_k_increment_passes(repo: Path) -> None:
    registry = _registry(repo)
    hyp = _hyp(registry, "H-admit")
    rel = _write_report(
        repo,
        "research/experiments/reports/h-admit-look1.md",
        sha=hyp["manifest_sha256"],
        look="yes",
    )
    hyp["test_looks"].append(
        {
            "seq": 1,
            "at": "2026-11-02",
            "manifest_sha256": hyp["manifest_sha256"],
            "report": rel,
            "report_sha256": _file_sha(repo, rel),
        }
    )
    hyp["k"] = 1
    _write_registry(repo, registry)
    assert lint.run(repo) == []


def test_look_without_k_increment_fails(repo: Path) -> None:
    registry = _registry(repo)
    hyp = _hyp(registry, "H-admit")
    rel = _write_report(
        repo,
        "research/experiments/reports/h-admit-look1.md",
        sha=hyp["manifest_sha256"],
        look="yes",
    )
    hyp["test_looks"].append(
        {
            "seq": 1,
            "at": "2026-11-02",
            "manifest_sha256": hyp["manifest_sha256"],
            "report": rel,
            "report_sha256": _file_sha(repo, rel),
        }
    )
    _write_registry(repo, registry)
    assert any("each look increments k" in p for p in lint.run(repo))


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda r: r["hypotheses"][0].update(extra=1), "unknown key 'extra'"),
        (lambda r: r["hypotheses"][0].pop("k"), "missing key 'k'"),
        (lambda r: r["hypotheses"][0].update(k=-1), "k must be an integer >= 0"),
        (lambda r: r["hypotheses"][0].update(status="live"), "status must be one of"),
        (lambda r: r["hypotheses"][0].update(markets=["jp"]), "markets must be"),
        (
            lambda r: r["hypotheses"][1].update(id=r["hypotheses"][0]["id"]),
            "duplicate id",
        ),
        (lambda r: r["hypotheses"][0].update(manifest_sha256="0" * 64), "hashes to"),
        (lambda r: r.update(schema_version=2), "schema_version must be 1"),
    ],
)
def test_registry_schema_violations_fail(repo: Path, mutate, needle: str) -> None:
    registry = _registry(repo)
    mutate(registry)
    _write_registry(repo, registry)
    problems = lint.run(repo)
    assert any(needle in p for p in problems), problems


def _rewrite_manifest(repo: Path, hid: str, mutate) -> None:
    registry = _registry(repo)
    hyp = _hyp(registry, hid)
    path = repo / hyp["manifest"]
    manifest = json.loads(path.read_text())
    manifest["version"] += 1  # a re-registered manifest is a new version
    mutate(manifest)
    path.write_text(json.dumps(manifest, indent=2))
    # Re-register the new version so only the schema rule under test can fail.
    hyp["previous_manifest_sha256"].append(hyp["manifest_sha256"])
    hyp["manifest_sha256"] = lint.canonical_sha256(manifest)
    _write_registry(repo, registry)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda m: m["splits"].update(gap_sessions=4),
            "gap_sessions must be an integer >= 5",
        ),
        (
            lambda m: m["splits"].update(purge_sessions=1),
            "below the longest label horizon",
        ),
        (lambda m: m.update(status="sealed"), "must not contain any pending value"),
        (lambda m: m.pop("stopping_rules"), "missing key 'stopping_rules'"),
        (
            lambda m: m["splits"]["test"].update(period=["2022-06-01", "2024-12-31"]),
            "test period must start after every validation period",
        ),
        (
            lambda m: m["splits"]["folds"][0].update(
                validate=["2018-06-01", "2019-12-31"]
            ),
            "train must end before validate starts",
        ),
        (lambda m: m["splits"].update(rules_doc="docs/other.md"), "rules_doc must be"),
        (lambda m: m.update(hypothesis_id="H-other"), "names hypothesis 'H-other'"),
    ],
)
def test_manifest_schema_violations_fail(repo: Path, mutate, needle: str) -> None:
    _rewrite_manifest(repo, "H-entry-price", mutate)
    problems = lint.run(repo)
    assert any(needle in p for p in problems), problems


def test_reregistered_manifest_version_passes(repo: Path) -> None:
    _rewrite_manifest(repo, "H-admit", lambda m: m.update(version=2))
    assert lint.run(repo) == []


def test_heartbeat_schema_violation_fails(repo: Path) -> None:
    path = repo / lint.HEARTBEAT_PATH
    config = json.loads(path.read_text())
    config["epochs"][0]["calendar"] = "LSE"
    path.write_text(json.dumps(config))
    assert any("calendar must be one of" in p for p in lint.run(repo))


def test_running_hypothesis_needs_heartbeat_epoch(repo: Path) -> None:
    registry = _registry(repo)
    _hyp(registry, "H-admit")["status"] = "running"
    _write_registry(repo, registry)
    problems = lint.run(repo)
    assert any("has no heartbeat epoch" in p for p in problems)


def _with_one_look(repo: Path) -> dict:
    registry = _registry(repo)
    hyp = _hyp(registry, "H-admit")
    rel = _write_report(
        repo,
        "research/experiments/reports/h-admit-look1.md",
        sha=hyp["manifest_sha256"],
        look="yes",
    )
    hyp["test_looks"] = [
        {
            "seq": 1,
            "at": "2026-11-02",
            "manifest_sha256": hyp["manifest_sha256"],
            "report": rel,
            "report_sha256": _file_sha(repo, rel),
        }
    ]
    hyp["k"] = 1
    return registry


def test_base_registry_k_cannot_decrease(repo: Path, tmp_path: Path) -> None:
    base = _with_one_look(repo)
    base_file = tmp_path / "base.json"
    base_file.write_text(json.dumps(base))
    # Head drops the look and resets k.
    problems = lint.run(repo, base_file)
    assert any("k decreased from 1 to 0" in p for p in problems)
    assert any("append only" in p for p in problems)


def test_base_registry_removal_fails(repo: Path, tmp_path: Path) -> None:
    base_file = tmp_path / "base.json"
    base_file.write_text((repo / lint.REGISTRY_PATH).read_text())
    registry = _registry(repo)
    registry["hypotheses"] = [h for h in registry["hypotheses"] if h["id"] != "H-admit"]
    _write_registry(repo, registry)
    assert any("was removed" in p for p in lint.run(repo, base_file))


def test_base_registry_growth_passes(repo: Path, tmp_path: Path) -> None:
    base_file = tmp_path / "base.json"
    base_file.write_text((repo / lint.REGISTRY_PATH).read_text())
    _write_registry(repo, _with_one_look(repo))
    assert lint.run(repo, base_file) == []


def test_print_hash_matches_registry(capsys) -> None:
    hyp = _hyp(_registry(REPO), "ROB-1411")
    assert lint.main(["--print-hash", str(REPO / hyp["manifest"])]) == 0
    assert capsys.readouterr().out.strip() == hyp["manifest_sha256"]


BAD = "a" * 64


@pytest.mark.parametrize(
    "line",
    [
        f"Experiment-Manifest-Sha256: {BAD}",
        f"\ufeffExperiment-Manifest-Sha256: {BAD}",
        f"Experiment-Manifest-Sha256: {BAD} (v1)",
        f"**Experiment-Manifest-Sha256:** {BAD}",
        f"- Experiment-Manifest-Sha256: {BAD}",
        f"    Experiment-Manifest-Sha256: {BAD}",
        f"experiment-manifest-sha256: {BAD}",
        f"Experiment-Manifest-Sha256: {BAD.upper()}",
        f"| Experiment-Manifest-Sha256 | {BAD} |",
        f"Experiment-Manifest-Sha256: {BAD}\r",
    ],
)
def test_decorated_unregistered_citation_still_fails(repo: Path, line: str) -> None:
    path = repo / "docs/research/probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# probe\n{line}\n", newline="")
    assert any("cites unregistered manifest hash" in p for p in lint.run(repo))


def test_malformed_citation_value_fails(repo: Path) -> None:
    path = repo / "docs/research/probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Experiment-Manifest-Sha256: abc123\n")
    assert any("malformed Experiment-Manifest-Sha256" in p for p in lint.run(repo))


def test_bold_test_look_is_still_counted(repo: Path) -> None:
    sha = _hyp(_registry(repo), "H-admit")["manifest_sha256"]
    path = repo / "docs/research/probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"Experiment-Manifest-Sha256: {sha}\n**Experiment-Test-Look:** yes\n"
    )
    assert any("does not count" in p for p in lint.run(repo))


def test_second_look_marker_fails(repo: Path) -> None:
    sha = _hyp(_registry(repo), "H-admit")["manifest_sha256"]
    path = repo / "docs/research/probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"Experiment-Manifest-Sha256: {sha}\n"
        "Experiment-Test-Look: yes\nExperiment-Test-Look: no\n"
    )
    problems = lint.run(repo)
    assert any("declared more than once" in p for p in problems)
    assert any("does not count" in p for p in problems)


def test_non_markdown_file_in_reports_dir_fails(repo: Path) -> None:
    path = repo / lint.REPORTS_DIR / "look1.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Experiment-Manifest-Sha256: {BAD}\n")
    assert any("must be .md" in p for p in lint.run(repo))


def test_counted_report_is_frozen(repo: Path, tmp_path: Path) -> None:
    registry = _with_one_look(repo)
    _write_registry(repo, registry)
    assert lint.run(repo) == []
    report = repo / "research/experiments/reports/h-admit-look1.md"
    report.write_text(report.read_text() + "Result: PASS, effect +2.1%\n")
    assert any("report_sha256 does not match" in p for p in lint.run(repo))


def test_duplicate_json_key_in_manifest_fails(repo: Path) -> None:
    path = repo / "research/experiments/manifests/h-admit.json"
    text = path.read_text()
    path.write_text(
        text.replace('"status": "draft"', '"status": "sealed", "status": "draft"', 1)
    )
    assert any("duplicate key 'status'" in p for p in lint.run(repo))


def test_manifest_change_without_version_bump_fails(repo: Path) -> None:
    registry = _registry(repo)
    hyp = _hyp(registry, "H-admit")
    path = repo / hyp["manifest"]
    manifest = json.loads(path.read_text())
    manifest["title"] = "changed"
    path.write_text(json.dumps(manifest))
    hyp["previous_manifest_sha256"].append(hyp["manifest_sha256"])
    hyp["manifest_sha256"] = lint.canonical_sha256(manifest)
    _write_registry(repo, registry)
    assert any("manifest version 1 must be 2" in p for p in lint.run(repo))


def test_status_cannot_move_backwards(repo: Path, tmp_path: Path) -> None:
    base = _registry(repo)
    _hyp(base, "H-admit")["status"] = "closed"
    base_file = tmp_path / "base.json"
    base_file.write_text(json.dumps(base))
    assert any("status moved backwards" in p for p in lint.run(repo, base_file))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["hypotheses"][0].update(manifest_sha256=[]),
        lambda r: r["hypotheses"][0].update(status=["draft"]),
        lambda r: r["hypotheses"][0].update(previous_manifest_sha256=5),
        lambda r: r["hypotheses"][0].update(test_looks=5),
    ],
)
def test_malformed_shapes_fail_cleanly(
    repo: Path, tmp_path: Path, mutate, capsys
) -> None:
    base_file = tmp_path / "base.json"
    base_file.write_text((repo / lint.REGISTRY_PATH).read_text())
    registry = _registry(repo)
    mutate(registry)
    _write_registry(repo, registry)
    assert lint.run(repo, base_file)  # problems reported, no exception
    assert lint.main(["--root", str(repo), "--base-registry", str(base_file)]) == 1
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("field", ["hypothesis", "calendar"])
def test_list_valued_heartbeat_field_fails_cleanly(repo: Path, field: str) -> None:
    path = repo / lint.HEARTBEAT_PATH
    config = json.loads(path.read_text())
    config["epochs"][0][field] = ["x"]
    path.write_text(json.dumps(config))
    assert lint.run(repo)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda m: m["splits"]["folds"][0].update(
                validate=["2019-01-01", "2019-02-31"]
            ),
            "validate must be",
        ),
        (lambda m: m["splits"].update(gap_sessions=5), "gap_sessions 5 is below"),
        (lambda m: m["cumulative_k"].update(prior_k=1), "cumulative_k.prior_k is 1"),
    ],
)
def test_review_round_manifest_rules(repo: Path, mutate, needle: str) -> None:
    _rewrite_manifest(repo, "H-entry-price", mutate)
    problems = lint.run(repo)
    assert any(needle in p for p in problems), problems


def test_impossible_registered_at_fails(repo: Path) -> None:
    registry = _registry(repo)
    registry["hypotheses"][0]["registered_at"] = "2026-02-30"
    _write_registry(repo, registry)
    assert any("registered_at must be a real date" in p for p in lint.run(repo))


def test_prior_k_must_count_looks_under_previous_hashes(repo: Path) -> None:
    registry = _with_one_look(repo)
    _write_registry(repo, registry)
    # New manifest version that forgets the look made under the old hash.
    _rewrite_manifest(repo, "H-admit", lambda m: None)
    assert any("cumulative_k.prior_k is 0" in p for p in lint.run(repo))
    _rewrite_manifest(repo, "H-admit", lambda m: m["cumulative_k"].update(prior_k=1))
    assert lint.run(repo) == []


def test_cited_report_without_look_marker_fails(repo: Path) -> None:
    sha = _hyp(_registry(repo), "H-admit")["manifest_sha256"]
    _write_report(repo, "docs/research/h-admit-note.md", sha=sha)
    assert any("must declare" in p for p in lint.run(repo))
