"""hk 3559: CI toolchains come from the Actions cache, pinned once, retried on a miss.

decision/2026-11-08/ci-toolchain-cache, rules 1-4:

1. Python, uv and Node are restored from the runner tool cache before their setup
   action, keyed by runner OS/arch and the exact pinned version. Every job that
   installs Python calls the one composite `.github/actions/setup-python-uv`; the
   Node job calls `.github/actions/setup-node-cached`. Nothing else under
   `.github/` may call `actions/setup-python`, `actions/setup-node` or `pip install uv`.
3. A cache miss retries the download with backoff; the composites own the attempts.
4. Every external `uses:` is pinned to a full commit SHA with the tag as a trailing
   comment, one SHA per action across the repo. Local `./` references are exempt.

Each test reads the YAML, so it pins what the workflows do, not how they are spelled.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"
PY_COMPOSITE = GITHUB / "actions" / "setup-python-uv" / "action.yml"
NODE_COMPOSITE = GITHUB / "actions" / "setup-node-cached" / "action.yml"
PY_USES = "./.github/actions/setup-python-uv"
NODE_USES = "./.github/actions/setup-node-cached"

SHA_RE = re.compile(
    r"^(?P<repo>[\w.-]+/[\w.-]+(?:/[\w.-]+)?)@(?P<sha>[0-9a-f]{40})(?P<rest>.*)$"
)
EXACT_RE = re.compile(r"^\d+\.\d+\.\d+$")
UV_INSTALL_RE = re.compile(
    r"\bpip\s+install\b[^\n]*\buv\b|\bpython\s+-m\s+pip\s+install\b[^\n]*\buv\b"
)
UV_USE_RE = re.compile(r"(^|\s|;|&|\|)uv\s+(sync|run|pip|tool|lock|export)\b", re.M)


def _yaml_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *GITHUB.rglob("action.yml")])


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(job_name, step) for every step of every job in a workflow document."""
    out: list[tuple[str, dict[str, Any]]] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            out.append((job_name, step))
    return out


def _uses_refs() -> list[tuple[Path, str]]:
    refs: list[tuple[Path, str]] = []
    for path in _yaml_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            m = re.match(r"^(?:- )?uses:\s*(\S+)", stripped)
            if m:
                refs.append((path, m.group(1)))
    return refs


# --- rule 4: SHA pins -------------------------------------------------------


def test_every_external_uses_is_a_full_sha_with_tag_comment() -> None:
    bad: list[str] = []
    for path, ref in _uses_refs():
        if ref.startswith("./"):
            continue
        if not SHA_RE.match(ref):
            bad.append(f"{path.relative_to(REPO_ROOT)}: {ref}")
    assert bad == [], "uses: not pinned to a 40-hex SHA: " + "; ".join(bad)


def test_every_pinned_uses_carries_its_version_comment() -> None:
    missing: list[str] = []
    for path in _yaml_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", line)
            if not m or m.group(1).startswith("./"):
                continue
            if not re.search(r"#\s*v\d", m.group(2)):
                missing.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()}")
    assert missing == [], "pinned uses: without a '# vN' comment: " + "; ".join(missing)


def test_one_action_is_pinned_to_one_sha_across_the_repo() -> None:
    shas: dict[str, set[str]] = {}
    for _path, ref in _uses_refs():
        m = SHA_RE.match(ref)
        if m:
            shas.setdefault(m.group("repo"), set()).add(m.group("sha"))
    split = {repo: sorted(v) for repo, v in shas.items() if len(v) > 1}
    assert split == {}, f"actions pinned to more than one SHA: {split}"


# --- rule 1: one composite for every Python toolchain ----------------------


def test_only_the_python_composite_calls_setup_python_or_installs_uv() -> None:
    offenders: list[str] = []
    for path in _yaml_files():
        if path in (PY_COMPOSITE, NODE_COMPOSITE):
            continue  # each composite is checked by its own tests below
        doc = _load(path)
        for job, step in _steps(doc):
            uses = str(step.get("uses", ""))
            run = str(step.get("run", ""))
            if uses.startswith("actions/setup-python@") or uses.startswith(
                "actions/setup-node@"
            ):
                offenders.append(f"{path.name}:{job}: direct {uses.split('@')[0]}")
            if UV_INSTALL_RE.search(run):
                offenders.append(
                    f"{path.name}:{job}: installs uv outside the composite"
                )
    assert offenders == [], "Python/uv installed outside the composite: " + "; ".join(
        offenders
    )


def test_every_job_that_runs_uv_sets_up_through_the_python_composite() -> None:
    missing: list[str] = []
    for path in WORKFLOWS.glob("*.yml"):
        doc = _load(path)
        for job_name, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if not steps:
                continue  # reusable-workflow caller job
            seen_setup = False
            for step in steps:
                if str(step.get("uses", "")) == PY_USES:
                    seen_setup = True
                run = str(step.get("run", ""))
                if UV_USE_RE.search(run) and not seen_setup:
                    missing.append(f"{path.name}:{job_name}")
                    break
    assert missing == [], "jobs run uv before the pinned composite: " + "; ".join(
        missing
    )


def test_python_composite_is_called_with_no_python_literal() -> None:
    """Callers never restate the Python or uv version; the composite is the one pin."""
    bad: list[str] = []
    for path in WORKFLOWS.glob("*.yml"):
        for job_name, step in _steps(_load(path)):
            if str(step.get("uses", "")) == PY_USES and step.get("with"):
                bad.append(f"{path.name}:{job_name}")
    assert bad == [], (
        "composite called with inputs (pins belong in action.yml): " + "; ".join(bad)
    )


def test_python_matrix_minor_matches_the_pinned_python() -> None:
    pinned = _composite_default(PY_COMPOSITE, "python-version")
    minor = ".".join(pinned.split(".")[:2])
    mismatched: list[str] = []
    for path in WORKFLOWS.glob("*.yml"):
        doc = _load(path)
        for job_name, job in (doc.get("jobs") or {}).items():
            for value in (job.get("strategy", {}).get("matrix", {}) or {}).get(
                "python-version", []
            ) or []:
                if str(value) != minor:
                    mismatched.append(f"{path.name}:{job_name}={value}")
    assert mismatched == [], f"matrix Python differs from pinned {pinned}: {mismatched}"


# --- rule 1 and 3: the pins and the retry shape ----------------------------


def _composite_default(path: Path, name: str) -> str:
    value = _load(path)["inputs"][name]["default"]
    return str(value)


def test_python_and_uv_are_pinned_to_exact_versions() -> None:
    assert EXACT_RE.match(_composite_default(PY_COMPOSITE, "python-version"))
    assert EXACT_RE.match(_composite_default(PY_COMPOSITE, "uv-version"))
    assert EXACT_RE.match(_composite_default(NODE_COMPOSITE, "node-version"))


def test_node_is_pinned_on_the_22_line_and_used_by_the_frontend_job() -> None:
    node = _composite_default(NODE_COMPOSITE, "node-version")
    assert node.startswith("22."), node
    doc = _load(WORKFLOWS / "test.yml")
    uses = [str(s.get("uses", "")) for _job, s in _steps(doc)]
    assert NODE_USES in uses
    assert not any(u.startswith("actions/setup-node@") for u in uses)


def _attempts(path: Path, action: str) -> list[dict[str, Any]]:
    steps = _load(path)["runs"]["steps"]
    return [s for s in steps if str(s.get("uses", "")).startswith(action)]


def test_python_setup_retries_three_times_and_only_the_last_attempt_is_fatal() -> None:
    attempts = _attempts(PY_COMPOSITE, "actions/setup-python@")
    assert len(attempts) == 3, [a.get("id") for a in attempts]
    assert attempts[0].get("continue-on-error") is True
    assert attempts[1].get("continue-on-error") is True
    assert not attempts[2].get("continue-on-error")


def test_node_setup_retries_three_times_and_only_the_last_attempt_is_fatal() -> None:
    attempts = _attempts(NODE_COMPOSITE, "actions/setup-node@")
    assert len(attempts) == 3, [a.get("id") for a in attempts]
    assert attempts[0].get("continue-on-error") is True
    assert attempts[1].get("continue-on-error") is True
    assert not attempts[2].get("continue-on-error")


def test_each_toolchain_is_restored_from_cache_before_its_setup_action() -> None:
    for composite, setup_prefix in (
        (PY_COMPOSITE, "actions/setup-python@"),
        (NODE_COMPOSITE, "actions/setup-node@"),
    ):
        steps = _load(composite)["runs"]["steps"]
        first_setup = next(
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith(setup_prefix)
        )
        cache_before = [
            s
            for s in steps[:first_setup]
            if str(s.get("uses", "")).startswith("actions/cache@")
        ]
        assert cache_before, f"{composite.parent.name}: no actions/cache before setup"
        key = str(cache_before[0]["with"]["key"])
        assert "runner.os" in key and "runner.arch" in key, key
        assert "inputs." in key, (
            f"{composite.parent.name}: cache key not tied to the exact version: {key}"
        )


def test_the_composite_restores_the_pinned_python_and_checks_it() -> None:
    steps = _load(PY_COMPOSITE)["runs"]["steps"]
    confirm = [s for s in steps if s.get("name") == "Confirm the pinned Python"]
    assert confirm and "platform.python_version()" in confirm[0]["run"]
