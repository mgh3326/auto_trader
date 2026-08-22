"""W5 — nothing in this repository turns the durable path on.

Adversarial review R24. Three gates default false in ``Settings``, but a
deployment surface can set an environment variable to ``true`` without any
Python default changing. So the negative is asserted where it actually lives:
across the tracked compose files, ops launchers and env templates.

The mutation test below is the point -- a guard that has never been shown a
file with the gate enabled is a guard that might be looking in the wrong
place.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[4]

#: Directories that never carry a deployment surface. Runbook prose, test
#: fixtures and application source all legitimately contain the literal
#: ``GATE=true``; reporting those would train a reader to ignore the guard.
#: The heavy ones (``.venv``, ``node_modules``) are here to keep the walk
#: cheap, not because they could arm anything.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".smoke-out",
        "htmlcov",
        "dist",
        "build",
        "site-packages",
        # deliberately out of scope, see above
        "tests",
        "docs",
        "research",
        "frontend",
        "app",
        "alembic",
    }
)

#: Filename patterns that are a deployment surface wherever they appear.
#: Matched at any depth, which is the R26 fix: ``scripts/*.sh`` missed
#: ``scripts/deploy/native/deploy-native.sh``, and no rule at all covered the
#: root launchers, the workflow shell scripts or the launchd plists.
_SURFACE_NAMES = (
    "env*.example",
    ".env*.example",
    "*.env",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "Dockerfile*",
    "Makefile",
    "*.sh",
    "*.plist",
    "*.plist.example",
    "*.service",
)

#: Directories whose contents are a deployment surface regardless of suffix.
_SURFACE_DIRS = (
    ".github/workflows",
    ".circleci",
    "ops",
)

W5_GATES = (
    "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
    "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
    "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
)

#: `GATE=true`, `GATE: true`, `GATE="true"`, `- GATE=1`, and so on.
_ENABLED = re.compile(
    r"(?P<gate>ORDER_PROPOSALS_TELEGRAM_CALLBACK_[A-Z_]*ENABLED)"
    r"\s*[:=]\s*[\"']?(?P<value>true|1|yes|on)\b",
    re.IGNORECASE,
)


def _is_deployment_surface(relative: pathlib.PurePosixPath) -> bool:
    """Closed-world allowlist: named surface, or inside a surface directory."""
    parent = relative.parent.as_posix()
    for directory in _SURFACE_DIRS:
        if parent == directory or parent.startswith(f"{directory}/"):
            return True
    return any(fnmatch.fnmatch(relative.name, rule) for rule in _SURFACE_NAMES)


def _deployment_files_in(root: pathlib.Path) -> list[pathlib.Path]:
    """Every file under ``root`` a deployment could read a gate from.

    A recursive walk with an explicit skip set, rather than a glob list:
    globs are enumerated by hand and therefore forget the surface nobody
    thought of, which is exactly how ``env.prod.example`` and the launchd
    plists went unread. Walking and *excluding* fails the other way -- a new
    deployment surface is covered the day it is added.
    """
    files: list[pathlib.Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = pathlib.PurePosixPath(path.relative_to(root).as_posix())
        if _SKIP_DIRS.intersection(relative.parts[:-1]):
            continue
        if _is_deployment_surface(relative):
            files.append(path)
    return sorted(set(files))


def _deployment_files() -> list[pathlib.Path]:
    return _deployment_files_in(_REPO)


def find_enabled_gates(text: str) -> list[str]:
    """Every W5 gate this text switches on."""
    return [match.group("gate") for match in _ENABLED.finditer(text)]


def test_the_guard_has_something_to_look_at() -> None:
    """Anti-vacuity: an empty file list would make the negative meaningless."""
    files = _deployment_files()
    assert len(files) >= 40, [str(path) for path in files]
    names = {path.name for path in files}
    assert "env.example" in names
    assert "env.prod.example" in names
    assert any(name.startswith("docker-compose") for name in names)
    assert any(name.endswith(".plist") for name in names)
    assert any(name.endswith(".sh") for name in names)


def test_no_deployment_surface_enables_a_w5_gate() -> None:
    """R24 — the actual no-auto-activation assertion."""
    offenders: list[str] = []
    for path in _deployment_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - binary in ops/
            continue
        for gate in find_enabled_gates(text):
            offenders.append(f"{path.relative_to(_REPO)}: {gate}")
    assert not offenders, offenders


def test_env_example_ships_every_gate_explicitly_false() -> None:
    """Present and false beats absent: an operator can see what exists."""
    text = (_REPO / "env.example").read_text(encoding="utf-8")
    for gate in W5_GATES:
        assert re.search(rf"^{gate}=false$", text, re.MULTILINE), gate


@pytest.mark.parametrize("gate", W5_GATES)
@pytest.mark.parametrize(
    "line",
    [
        "{gate}=true",
        "{gate}: true",
        '{gate}="true"',
        "      - {gate}=1",
        "{gate}=on",
    ],
)
def test_the_guard_would_catch_an_enabled_gate(gate: str, line: str) -> None:
    """Mutation: put the gate in a deployment file and the guard must fire.

    Asserted against the matcher directly rather than by writing into a
    tracked file, so the test cannot leave a deployment surface armed if it
    fails part-way.
    """
    mutated = f"services:\n  api:\n    environment:\n      {line.format(gate=gate)}\n"
    assert find_enabled_gates(mutated) == [gate], mutated


@pytest.mark.parametrize("gate", W5_GATES)
def test_the_guard_does_not_fire_on_a_disabled_gate(gate: str) -> None:
    for line in (f"{gate}=false", f"{gate}: false", f'{gate}=""', f"# {gate}=true"):
        # A commented-out enable is still an enable as far as this guard is
        # concerned -- deliberately, because uncommenting is one keystroke.
        expected = [gate] if line.startswith("#") else []
        assert find_enabled_gates(line) == expected, line


# ---------------------------------------------------------------------------
# R26 -- the discovery, not just the matcher
# ---------------------------------------------------------------------------
#
# The mutation tests above prove the *regex* fires. They say nothing about
# whether the guard ever reads the file the gate was written into, and the
# glob list missed several tracked surfaces that really do get read at
# deployment time: ``env.prod.example``, the root launchers, the shell scripts
# under ``.github/workflows/``, the launchd plists, and anything nested deeper
# than ``scripts/*.sh``. A gate set in any of those passed the guard.
#
# So these drive the discovery itself against a repo-shaped tree.

#: Tracked path classes the original glob list could not see.
OMITTED_SURFACES = (
    "env.prod.example",
    "run_docker.sh",
    "run_api_compose.sh",
    ".github/workflows/taskiq-smoke.sh",
    "ops/native/plists/com.robinco.auto-trader.worker.plist",
    "ops/native/scripts/run-scheduler.sh",
    "scripts/deploy/native/deploy-native.sh",
)

#: Path classes the original glob list already covered. Kept so the fix has
#: to be an extension rather than a replacement.
COVERED_SURFACES = (
    "env.example",
    "Makefile",
    "docker-compose.prod.yml",
    "scripts/run_taskiq_worker.sh",
    ".github/workflows/deploy.yml",
)

#: Places a fake gate literal legitimately lives. Scanning them would make
#: the guard noisy rather than stronger, so they must stay out.
NON_DEPLOYMENT_SURFACES = (
    "tests/services/order_proposals/callback_inbox/test_no_auto_activation.py",
    "docs/runbooks/telegram-callback-durable-inbox.md",
    "app/core/config.py",
    "frontend/invest/.env.local.example",
    "research/notes.md",
)


def _repo_shaped_tree(root: pathlib.Path) -> None:
    """A minimal tree with one file per path class, all gates false."""
    for relative in OMITTED_SURFACES + COVERED_SURFACES + NON_DEPLOYMENT_SURFACES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(f"{gate}=false" for gate in W5_GATES) + "\n",
            encoding="utf-8",
        )


def _scan(root: pathlib.Path) -> list[str]:
    """Whatever the production guard reports for this tree."""
    offenders: list[str] = []
    for path in _deployment_files_in(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - binary
            continue
        for gate in find_enabled_gates(text):
            offenders.append(f"{path.relative_to(root).as_posix()}: {gate}")
    return sorted(offenders)


@pytest.mark.parametrize("relative", OMITTED_SURFACES + COVERED_SURFACES)
def test_discovery_reaches_every_deployment_surface(
    relative: str, tmp_path: pathlib.Path
) -> None:
    """R26 — plant a live gate in each tracked path class; the guard must see it.

    This is the assertion the regex mutation tests could not make: a guard
    that never opens ``env.prod.example`` will pass every matcher test ever
    written and still let the gate through.
    """
    _repo_shaped_tree(tmp_path)
    gate = "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED"
    (tmp_path / relative).write_text(f"{gate}=true\n", encoding="utf-8")

    assert _scan(tmp_path) == [f"{relative}: {gate}"]


@pytest.mark.parametrize("relative", NON_DEPLOYMENT_SURFACES)
def test_discovery_ignores_non_deployment_files(
    relative: str, tmp_path: pathlib.Path
) -> None:
    """The allowlist is closed both ways: no indiscriminate tree scan.

    Test fixtures, runbook prose and application source all legitimately
    contain the literal ``GATE=true``. Reporting those would train a reader
    to ignore the guard, which is worse than not having it.
    """
    _repo_shaped_tree(tmp_path)
    gate = "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED"
    (tmp_path / relative).write_text(f"{gate}=true\n", encoding="utf-8")

    assert _scan(tmp_path) == []


def test_discovery_is_anchored_on_the_real_repository() -> None:
    """The path classes above are not hypothetical -- each exists here."""
    for relative in (
        "env.prod.example",
        ".github/workflows/taskiq-smoke.sh",
        "ops/native/plists/com.robinco.auto-trader.worker.plist",
        "ops/native/scripts/run-scheduler.sh",
    ):
        assert (_REPO / relative).is_file(), relative

    discovered = {
        path.relative_to(_REPO).as_posix() for path in _deployment_files_in(_REPO)
    }
    for relative in (
        "env.example",
        "env.prod.example",
        "Makefile",
        ".github/workflows/taskiq-smoke.sh",
        "ops/native/plists/com.robinco.auto-trader.worker.plist",
        "ops/native/scripts/run-scheduler.sh",
    ):
        assert relative in discovered, relative

    # ... and it still refuses to wander into prose or fixtures.
    assert not [item for item in discovered if item.startswith(("docs/", "tests/"))]


def test_the_real_repository_enables_no_gate_anywhere() -> None:
    """R26 — the widened scan, run for real."""
    assert _scan(_REPO) == []
